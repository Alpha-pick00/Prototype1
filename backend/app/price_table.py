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


async def _search_elevenst_items(query: str, limit: int) -> list[elevenst.ElevenstSearchItem]:
    """app.debate.check_clarify_facets의 상품명 표본 출처 - 막히거나 실패해도
    예외를 던지지 않고 빈 리스트를 반환한다(호출자가 폴백을 따로 두지 않아도
    되도록)."""
    try:
        return await elevenst.search_elevenst(query, limit=limit)
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
