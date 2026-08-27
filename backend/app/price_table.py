"""11번가 검색 결과를 파이프라인에 연결하는 계층 - 상품명 관련성 판정
(_product_name_matches)과 app.debate.check_clarify_facets가 쓰는 11번가
검색/카테고리 조회 래퍼를 담는다."""

from __future__ import annotations

import logging

from rapidfuzz import fuzz

from fetchers import elevenst
from fusion.dedup import NAME_SIMILARITY_THRESHOLD

from . import embeddings
from .exclusive_tokens import exclusive_tokens_conflict
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


async def _search_elevenst_items(
    query: str, limit: int, sort_cd: str = "A"
) -> list[elevenst.ElevenstSearchItem]:
    """app.debate.check_clarify_facets의 상품명 표본 출처 - 막히거나 실패해도
    예외를 던지지 않고 빈 리스트를 반환한다(호출자가 폴백을 따로 두지 않아도
    되도록). sort_cd가 기본값(A)이면 kwarg 자체를 안 넘긴다 - _search_candidates와
    같은 이유로, 이 함수를 patch하는 기존 테스트 더블(sort_cd를 모르는 단순
    (query, limit=10) 시그니처)이 깨지지 않게 하기 위함이다."""
    try:
        kwargs = {} if sort_cd == "A" else {"sort_cd": sort_cd}
        return await elevenst.search_elevenst(query, limit=limit, **kwargs)
    except elevenst.ElevenstSearchBlocked:
        logger.warning("elevenst search blocked for query=%r", query)
        return []
    except Exception:
        logger.exception("elevenst search crashed for query=%r", query)
        return []


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
    "케이스", "커버", "파우치", "스킨", "필름", "강화유리", "보호필름",
    "거치대", "마운트", "홀더", "그립", "그립톡", "스탠드",
    "충전기", "충전패드", "충전케이블", "케이블", "젠더", "어댑터",
    "이어폰", "이어버드", "헤드폰", "헤드셋", "이어훅", "이어팁",
    "스트랩", "고리", "범퍼", "젤리케이스", "하드케이스", "배터리",
    # 2026-08-26 실측("아이폰 16 256GB" 드릴다운 - 추천도순 표본이 액정보호
    # 필름 일색이었던 순간, 액세서리 제거 후 남은 2개마저 "셀카 스틱 삼각대"/
    # "카메라렌즈 보호"였다) - 카메라·촬영 액세서리 쪽 커버리지 보강.
    "삼각대", "셀카봉", "셀카스틱", "짐벌", "렌즈",
}


def _looks_like_accessory(product_name: str) -> bool:
    return any(token in product_name for token in _ACCESSORY_INDICATOR_TOKENS)


def all_candidates_look_like_accessories(query: str, items: list[elevenst.ElevenstSearchItem]) -> bool:
    """`_search_candidates`가 sortCd="H" 보정 검색을 태울지 판단하는 트리거.
    질의 자체가 액세서리를 찾는 거면("아이폰 케이스") 트리거하지 않는다 -
    그 경우 액세서리만 나오는 게 정상이다."""
    if not items or _looks_like_accessory(query):
        return False
    return all(_looks_like_accessory(it["product_name"]) for it in items)


# 2026-08-26 실측(AI 상세검색 "아이폰 16" - 액세서리를 걸러내고 나니 남은 9개
# 표본이 전부 "S+등급 아이폰 16 프로 256 ... 중고폰 공기계"류 중고 매물이었다.
# 신품 매물은 "추천도순" 상위 90개 안에 아예 없었다 - 액세서리와 마찬가지로
# 저가 매물이 추천도순 상위를 차지하는 현상). 이게 왜 문제냐면, 중고폰
# 매물은 용량을 "256GB"가 아니라 "256"처럼 단위 없이 쓰는 경우가 흔해서
# (신품 정식 매물은 거의 항상 "GB"를 붙여 쓴다), facet 추출용 표본이 전부
# 중고 매물이면 "용량" facet 값 자체에 "GB" 단위가 빠져버린다.
_USED_CONDITION_INDICATOR_TOKENS = {"중고", "중고폰", "공기계", "리퍼", "리퍼비시", "리퍼상품"}


def _looks_like_used_condition(product_name: str) -> bool:
    return any(token in product_name for token in _USED_CONDITION_INDICATOR_TOKENS)


def all_candidates_look_like_used_condition(query: str, items: list[elevenst.ElevenstSearchItem]) -> bool:
    """`check_clarify_facets`가 sortCd="H" 보정 검색을 태울지 판단하는
    트리거(all_candidates_look_like_accessories와 같은 논리). 질의 자체가
    중고를 찾는 거면("아이폰 16 중고") 트리거하지 않는다."""
    if not items or _looks_like_used_condition(query):
        return False
    return all(_looks_like_used_condition(it["product_name"]) for it in items)


# 2026-08-26 실측("아이폰 16/17" 데모 검증) - all_candidates_look_like_accessories가
# sortCd="H" 보정 검색을 태워 본품을 찾아와도, 원래 있던 액세서리 후보들이
# 그대로 후보 풀에 남아있었다. "아이폰 16 프로 클리어케이스"처럼 액세서리
# 상품명에도 검색어가 그대로 들어있어서 임베딩 관련도 순위에서 본품과
# 경쟁하거나 이길 수 있다 - 상위로 올라오면 추천/후보 목록에 케이스가 낀다.
# 질의 자체가 액세서리를 찾는 게 아니면, 이미 관련성 필터를 통과한 후보
# 중에서도 액세서리 상품명은 후보 풀에서 아예 제외한다(all_candidates_
# look_like_accessories와 같은 단어 목록 재사용 - "전부 액세서리인지" 판단이
# 아니라 "개별 후보가 액세서리인지" 판단으로 씀).
def filter_out_accessory_noise(
    query: str, items: list[elevenst.ElevenstSearchItem]
) -> list[elevenst.ElevenstSearchItem]:
    """질의가 액세서리를 찾는 게 아니면 후보 중 액세서리 상품명을 제외한다.
    제외하고 나면 하나도 안 남는 경우(진짜 액세서리밖에 없거나, 단어 목록이
    본품도 오탐한 경우)는 원래 목록을 그대로 돌려준다 - 하드 필터가 아니라
    "더 나은 표본이 있으면 그걸 쓴다"는 소프트 필터."""
    if _looks_like_accessory(query):
        return items
    non_accessories = [it for it in items if not _looks_like_accessory(it["product_name"])]
    return non_accessories or items


def _median_price(items: list[elevenst.ElevenstSearchItem]) -> float:
    prices = sorted(it["price_krw"] for it in items)
    n = len(prices)
    mid = n // 2
    if n % 2 == 0:
        return (prices[mid - 1] + prices[mid]) / 2
    return prices[mid]


# 2026-08-26 실측("아이폰 16" 검색 - 관련성 필터를 통과한 정상 매물 18개가
# 전부 388만~500만원대인데, 딱 2개만 2,515만원/2,998만원짜리가 섞여
# 있었다 - 정상가 대비 5~7배, 광고성/오류 매물로 보인다. "아이폰 17"은
# 22개 전부 399만~552만원 사이로 이런 이상치가 없었다 - 그 경계를 정확히
# 가르는 배수로 3을 쓴다). 관련성·모델·수량까지 다 통과한 후보라도 가격이
# 나머지 표본과 동떨어지게 비싸면 실제 판매가가 아닐 가능성이 높다 - 표본이
# 너무 작으면(3개 미만) 중앙값 판단 자체가 불안정하므로 건너뛴다.
_PRICE_OUTLIER_MEDIAN_MULTIPLIER = 3


def filter_price_outliers(items: list[elevenst.ElevenstSearchItem]) -> list[elevenst.ElevenstSearchItem]:
    """중앙값의 `_PRICE_OUTLIER_MEDIAN_MULTIPLIER`배를 넘는 가격의 후보를
    제외한다. 전부 제외되면(표본 자체가 다 튀는 경우) 원래 목록을 그대로
    돌려준다 - 소프트 필터."""
    if len(items) < 3:
        return items
    median = _median_price(items)
    if median <= 0:
        return items
    kept = [it for it in items if it["price_krw"] <= median * _PRICE_OUTLIER_MEDIAN_MULTIPLIER]
    return kept or items


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
    모델/수량/배타 속성 차이(예: 아이폰6 vs 아이폰15, 백미 vs 현미)를 못
    잡으므로, 기존 가드(model_or_quantity_conflict, exclusive_tokens_conflict)는
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
        matches.append(it)
    return matches
