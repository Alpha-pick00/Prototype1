"""11번가 검색 결과를 파이프라인에 연결하는 계층 - 상품명 관련성 판정
(_product_name_matches)과 app.debate.check_clarify_facets가 쓰는 11번가
검색/카테고리 조회 래퍼를 담는다."""

from __future__ import annotations

import logging
import statistics

from rapidfuzz import fuzz

from fetchers import elevenst
from fusion.dedup import NAME_SIMILARITY_THRESHOLD

from . import embeddings
from .exclusive_tokens import accessory_mismatch, exclusive_tokens_conflict
from .spec_match import model_or_quantity_conflict

logger = logging.getLogger(__name__)

# check_clarify_facets() 전용(사용자 요청, 2026-08-12: "브랜드가 2,3개 정도만
# 뜨는데") - 상세페이지를 추가로 페치하지 않고(가격표 실측이 아니라 facet
# 추출용 상품명만 필요) 검색 결과 상품명을 최대한 많이 확보해야 브랜드/기종별
# 다양성이 나온다.
CLARIFY_SEARCH_LIMIT = 90

# 단발 질의(드릴다운 없이 바로 검색) 전용(2026-08-25 사용자 리포트 - "초콜릿
# 사고싶어 하면 책이 뜨고 초콜릿만 치면 상품이 나온다") - 11번가 검색은
# 가격 오름차순(sortCd=A)이라 최저가 10개 안에 "공병호의 초콜릿"류(제목에
# 질의어가 그대로 들어간 헐값 중고책)처럼 카테고리는 다른데 이름만 우연히
# 겹치는 상품이 섞이면, _product_name_matches(단순 이름 매칭)를 통과하는
# 후보가 그것 하나뿐이라 추천 Agent에게 다른 선택지가 아예 없었다. 표본을
# 넓히면 실제 초콜릿류가 함께 걸릴 확률이 올라가 추천 Agent(과 임베딩 관련도
# 정렬)가 그중에서 고를 수 있다 - HTTP 요청은 그대로 1번이라(pageSize만
# 커짐) 지연에 미치는 영향은 거의 없다.
SINGLE_QUERY_SEARCH_LIMIT = 30


async def _search_elevenst_items(query: str, limit: int) -> list[elevenst.ElevenstSearchItem]:
    """app.debate.check_clarify_facets의 상품명 표본 출처 - 막히거나 실패해도
    예외를 던지지 않고 빈 리스트를 반환한다(호출자가 폴백을 따로 두지 않아도
    되도록).

    보정 검색(2026-08-26, "아이폰 17"을 치면 되묻는 옵션이 폰 스펙이 아니라
    "케이스형태"/"패턴" 같은 액세서리 축으로 나오던 문제) - facet 추출용
    표본이 전부 액세서리 지시어를 달고 있으면 sortCd="H"(높은가격순)로 한 번
    더 찾아 합친다. `_search_candidates`가 메인 검색에서 쓰는 것과 동일한
    트리거(most_candidates_look_like_accessories)다 - 표본 자체가 액세서리
    투성이면 거기서 뽑히는 facet도 액세서리 축(케이스/충전기 등)일 수밖에
    없으므로, 메인 검색과 같은 이유로 여기도 보정이 필요하다."""
    try:
        items = await elevenst.search_elevenst(query, limit=limit)
    except elevenst.ElevenstSearchBlocked:
        logger.warning("elevenst search blocked for query=%r", query)
        return []
    except Exception:
        logger.exception("elevenst search crashed for query=%r", query)
        return []

    if most_candidates_look_like_accessories(query, items):
        try:
            high_price_items = await elevenst.search_elevenst(query, limit=limit, sort_cd="H")
        except Exception:
            logger.exception("elevenst 보정 검색(sortCd=H) 실패 for query=%r", query)
            return items
        items = _dedupe_by_product_code(items + high_price_items)
    return items


async def _search_elevenst_categories(query: str) -> list[elevenst.ElevenstCategoryGroup]:
    """app.debate.check_clarify_facets의 "카테고리" facet 실측 출처 - 같은
    예외 안전성 계약(막히거나 실패해도 빈 리스트)."""
    try:
        return await elevenst.search_categories(query)
    except elevenst.ElevenstSearchBlocked:
        logger.warning("elevenst category breakdown search blocked for query=%r", query)
        return []
    except Exception:
        logger.exception("elevenst category breakdown search crashed for query=%r", query)
        return []


# 2026-08-24 실측("아이간식" 검색 시 "자동 제면기 파스타 기계 식사준비
# 건강식 아이간식 식당 업소용..."이 관련 상품으로 통과) - token_set_ratio는
# 질의 토큰이 후보 상품명에 전부 부분집합으로 들어있기만 하면 나머지가
# 아무리 길고 무관해도(키워드 도배) 100점을 준다. token_sort_ratio는
# 길이·순서 차이에 민감해서 이런 도배 상품명을 가려낸다 - 실측: 진짜
# "아이간식" 떡 상품 34~38점, 무관한 파스타 기계 16.7점.
#
# 20이 아니라 18인 이유(회귀 발견, test_query_variants) - "이프로"(3글자
# 짧은 질의)처럼 검색어 자체가 짧고 후보 상품명이 정상적으로 길기만 해도
# (도배가 아니라 그냥 상세한 상품명) token_sort_ratio가 자연히 낮게 나온다
# (실측: "이프로" vs "이프로 부족할때 제로 복숭아 500ml x 24개" = 19.99점 -
# 20 기준이면 정상 매칭을 걸러내는 회귀가 생겼다). 18로 낮추면 그 정상
# 케이스(19.99)는 통과하면서 파스타 기계(16.7)는 여전히 걸러진다 - 두
# 실측값을 정확히 가르는 경계.
_MIN_TOKEN_SORT_RATIO = 18


def _product_name_matches(decision_name: str, candidate_name: str) -> bool:
    """검색 결과 상품명(candidate_name)이 실제로 질의/결정된 상품명
    (decision_name)과 같은 상품인지 판정한다 - app.debate의
    run_elevenst_only_debate/run_brand_price가 검색 결과를 후보로 받아들이기
    전 관련성 가드로 쓴다."""
    if fuzz.token_set_ratio(decision_name, candidate_name) < NAME_SIMILARITY_THRESHOLD:
        return False
    if fuzz.token_sort_ratio(decision_name, candidate_name) < _MIN_TOKEN_SORT_RATIO:
        return False
    if model_or_quantity_conflict(decision_name, candidate_name):
        return False
    # 백미/발아현미처럼 순한글 단어 하나가 결정적 차이인 경우 - token_set_ratio는
    # 공통 토큰이 많으면 이런 차이를 그냥 덮어버린다(실측 93.0점, 85 통과).
    if exclusive_tokens_conflict(decision_name, candidate_name):
        return False
    # 기기 본체를 찾는데 그 기기용 케이스/충전기 등 부속품이 섞여 나오는 경우
    # (2026-08-25 사용자 리포트 - "아이폰 17 256gb 자급제"에 "아이폰 15 케이스"
    # 추천됨). exclusive_tokens_conflict와 달리 질의 쪽엔 신호가 없는 게
    # 정상이라 비대칭으로 따로 판정한다.
    if accessory_mismatch(decision_name, candidate_name):
        return False
    return True


# 2026-08-26 실측("아이폰 17" 검색 - 추천도순(sortCd=A) 표본 30개가 전부
# "OO용 케이스/충전기/거치대"류였고 본품은 하나도 없었다. 인기·고가 상품은
# 액세서리 판매자들이 표기에 상품명을 끼워 넣는 경우가 많은데, 가격 낮은
# 것부터 잡히는 추천도순/오름차순에선 본품(비쌈)이 표본 밖으로 밀려난다) -
# 관련성 필터를 통과한 후보 전부가 이 목록의 단어를 달고 있으면 "본품이
# 표본에 없을 가능성이 높다"는 로컬 신호로 쓴다(네트워크/LLM 호출 없음).
# 카테고리를 한정하는 하드 필터로는 못 쓴다(신발끈·리필 같은 다른 카테고리
# 액세서리는 안 잡힌다 - 그건 challenge(DeepSeek)가 의미로 이미 일반적으로
# 잡아준다) - 여기서는 "H(높은가격순) 보정 검색을 태울지" 판단하는 트리거로만
# 쓴다. 트리거가 과하게(오탐으로) 걸려도 피해는 검색 1번 더 하는 정도라
# 하드 필터보다 리스크가 훨씬 낮다.
_ACCESSORY_INDICATOR_TOKENS = {
    "케이스", "커버", "파우치", "스킨", "필름", "강화유리", "보호필름", "보호대",
    "거치대", "마운트", "홀더", "그립", "그립톡", "스탠드", "렌즈",
    "충전기", "충전패드", "충전케이블", "케이블", "젠더", "어댑터",
    "이어폰", "이어버드", "헤드폰", "헤드셋", "이어훅", "이어팁",
    "스트랩", "고리", "범퍼", "젤리케이스", "하드케이스", "배터리",
}


def _looks_like_accessory(product_name: str) -> bool:
    return any(token in product_name for token in _ACCESSORY_INDICATOR_TOKENS)


# 단어 목록은 구조적으로 늘 커버리지가 부족하다(실측 - "설치 카메라 렌즈
# 보호대(포지셔닝 프레임...)"처럼 목록에 없는 표현을 쓴 액세서리가 섞여
# 있으면 all()이 트리거를 놓친다 - 90개 표본 중 딱 2개만 안 걸려도 전체가
# False가 됐다). "전부"가 아니라 "대다수"가 액세서리로 보이면 트리거하도록
# 완화한다 - 표본에 어차피 안 걸리는 소수 표현이 몇 개 섞여도 흔들리지 않는다.
_ACCESSORY_TRIGGER_RATIO = 0.9


def most_candidates_look_like_accessories(query: str, items: list[elevenst.ElevenstSearchItem]) -> bool:
    """`_search_candidates`/`_search_elevenst_items`가 sortCd="H" 보정 검색을
    태울지 판단하는 트리거. 질의 자체가 액세서리를 찾는 거면("아이폰 케이스")
    트리거하지 않는다 - 그 경우 액세서리만 나오는 게 정상이다."""
    if not items or _looks_like_accessory(query):
        return False
    accessory_count = sum(1 for it in items if _looks_like_accessory(it["product_name"]))
    return accessory_count / len(items) >= _ACCESSORY_TRIGGER_RATIO


def _dedupe_by_product_code(items: list[elevenst.ElevenstSearchItem]) -> list[elevenst.ElevenstSearchItem]:
    """product_code 기준 중복 제거(없으면 url, 그마저 없으면 상품명) - 먼저
    나온 항목을 남긴다. `_search_candidates`가 정렬이 다른 두 검색 결과를
    합칠 때 같은 상품이 양쪽에 다 잡히는 경우를 정리한다."""
    seen: set[str] = set()
    deduped: list[elevenst.ElevenstSearchItem] = []
    for item in items:
        key = item.get("product_code") or item.get("url") or item.get("product_name", "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


# 2026-08-24 실측(사용자 리포트 "망고주스를 사고 싶어 검색이 안 됨") -
# _product_name_matches는 공백으로 나눈 토큰이 문자 그대로 겹쳐야 점수가
# 나온다. "망고주스"(질의, 붙여쓰기)와 11번가 실제 표기 "카프리썬 오렌지망고
# 200ml x 40입 주스"(단어가 쪼개지고 순서도 다름)는 같은 상품인데 토큰이
# 하나도 안 겹쳐 token_set_ratio 19.5점(임계값 85)으로 전부 걸러졌다 - 이건
# "이프로"류(표기가 아예 다른 케이스)와 달리 표기 자체는 맞지만 띄어쓰기
# 단위가 다른 경우라 _search_with_query_variants(HCX 대안 표기)로도 못
# 구제한다. 임베딩 코사인 유사도는 띄어쓰기/순서와 무관하게 의미로
# 비교하므로 이 케이스를 구제한다 - 실측: 진짜 망고주스류 0.65~0.72,
# 무관하거나 결이 다른 상품(사과 드링크, 과일향 차음료) 0.50~0.56.
_SEMANTIC_FALLBACK_THRESHOLD = 0.6


async def semantic_relevance_fallback(
    query: str, items: list[elevenst.ElevenstSearchItem]
) -> list[elevenst.ElevenstSearchItem]:
    """_product_name_matches가 하나도 못 찾았을 때만 쓰는 2차 관련성 판정
    (app.debate._search_and_rank_candidates 참고) - 순수 의미 유사도만으로는
    모델/수량/배타 속성 차이(예: 아이폰6 vs 아이폰15, 백미 vs 현미)나 본체 vs
    부속품 차이(예: 아이폰 자체 vs 아이폰 케이스)를 못 잡으므로, 기존 가드
    (model_or_quantity_conflict, exclusive_tokens_conflict, accessory_mismatch)는
    그대로 적용해서 통과시킨다 - 표기 차이는 구제하되 진짜 다른 상품까지
    구제하지 않기 위함."""
    if not items:
        return []
    names = [it["product_name"] for it in items]
    query_vec = await embeddings.embed([query])
    item_vecs = await embeddings.embed(names)
    if query_vec is None or item_vecs is None:
        return []
    qv = query_vec[0]
    matches = []
    for it, name, vec in zip(items, names, item_vecs):
        if embeddings.cosine_similarity(qv, vec) < _SEMANTIC_FALLBACK_THRESHOLD:
            continue
        if model_or_quantity_conflict(query, name):
            continue
        if exclusive_tokens_conflict(query, name):
            continue
        if accessory_mismatch(query, name):
            continue
        matches.append(it)
    return matches


# 2026-08-25 사용자 리포트("말이 안되는 가격을 추천하면 안되겠지?... 왜
# 200만원이 넘는걸 추천한거지?" 이후, "왜 이제 75만원짜리를 추천하냐"로
# 이어진 케이스) - 아이폰 17처럼 막 나온 신제품은 11번가 리스팅 전부가
# 리뷰·구매만족도 0으로 동률인 경우가 흔해(실측 확인) recommend Agent가
# 그 두 신호로 판매자를 구분할 방법이 없다. 처음엔 이 판정을 프롬프트에
# 경고 문구로만 얹었는데(agents/base.py), HCX-005가 그 문구를 반복
# 무시하고 순수 최저가만 골랐다(같은 요청 2회 재현, 매번 동일 후보 선택) -
# 텍스트 경고로는 이 모델을 못 믿는다는 뜻이라, 아예 이 신호를 코드에서
# 구조적으로 반영한다(agents/base.py가 프롬프트 표시용으로 재사용).
_SUSPICIOUS_PRICE_RATIO = 0.5

# "미개봉"/"완납"은 정상적인 자급제 새 제품에도 쓰이는 말이라(100% 확실한
# 신호는 아님) 완전 배제가 아니라 후순위화 근거로만 쓴다.
_CONTRACT_SUSPICION_TERMS = ("미개봉", "완납")


def median_price(items: list[elevenst.ElevenstSearchItem]) -> float:
    prices = [it["price_krw"] for it in items]
    return statistics.median(prices) if prices else 0.0


def is_price_suspicious(price_krw: int, median: float) -> bool:
    return median > 0 and price_krw < median * _SUSPICIOUS_PRICE_RATIO


def has_contract_suspicion_phrase(product_name: str) -> bool:
    return any(term in product_name for term in _CONTRACT_SUSPICION_TERMS)


def is_trust_suspicious(item: elevenst.ElevenstSearchItem, median: float) -> bool:
    """가격이 이 후보군 중앙값의 절반 미만이거나(is_price_suspicious), 상품명에
    통신사 약정 상품에 흔한 문구가 있으면(has_contract_suspicion_phrase) True."""
    return is_price_suspicious(item["price_krw"], median) or has_contract_suspicion_phrase(item["product_name"])


def deprioritize_suspicious(
    items: list[elevenst.ElevenstSearchItem],
) -> list[elevenst.ElevenstSearchItem]:
    """의심스러운 후보를 완전히 배제하지 않고 뒤로 미룬다 - 전부 의심스러우면
    (예: 이 검색어 자체가 원래 파격 세일 상품군) 안정 정렬 특성상 결과가
    그대로 유지되므로, 후보가 하나도 안 남는 사고가 나지 않는다. 각 그룹
    안에서는 기존 순서(임베딩 관련도순)를 그대로 유지한다(list.sort는
    안정 정렬)."""
    if not items:
        return items
    median = median_price(items)
    return sorted(items, key=lambda it: is_trust_suspicious(it, median))
