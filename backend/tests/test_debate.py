from __future__ import annotations

import asyncio

from app import debate
from app.debate import (
    _FACET_ORDER_HINTS,
    _attach_facet_crossfilter,
    _build_facet_value_incidence,
    _facet_centrality,
    _facet_resolved,
    _facet_sort_key,
    _is_ambiguous_facets,
    _resolved_facet_count,
    _search_candidates,
    _strip_cross_brand_options,
    _strip_resolved_facets,
)
from app.schemas import ClarifyFacet
from fetchers.elevenst import ElevenstSearchItem


def _facet(label: str, options: list[str]) -> ClarifyFacet:
    return ClarifyFacet(label=label, options=options)


def _item(name: str, price: int = 1000, code: str = "1") -> ElevenstSearchItem:
    return ElevenstSearchItem(
        product_code=code,
        product_name=name,
        price_krw=price,
        seller="판매자",
        url=f"https://www.11st.co.kr/products/{code}",
        review_count=None,
        buy_satisfy=None,
        image_url=None,
    )


# ---------------------------------------------------------------------------
# _search_candidates - sortCd="H" 보정 검색 트리거(2026-08-26, "아이폰 17"
# 실측). 단발 질의(base_query 없음) 경로만 다룬다 - 드릴다운 경로는 구조적
# 필터링이라 이 트리거 대상이 아니다.
# ---------------------------------------------------------------------------


def test_search_candidates_does_not_rescue_when_relevant_items_already_found(monkeypatch):
    """평소(대부분의) 검색은 보정 검색이 아예 안 걸려야 한다 - 추가 지연/
    비용이 생기면 안 된다.

    상품명은 _ACCESSORY_INDICATOR_TOKENS(price_table.py)와 안 겹치는 상품으로
    고른다 - 원래 픽스처(이어버드)는 "이어버드"가 그 목록에 실제 액세서리
    낱말로도 들어있어(이어폰/헤드폰과 함께, 케이스에 담긴 이어폰처럼 진짜
    액세서리일 때 잡으려는 용도), 정상적으로 관련 상품이 있어도
    most_candidates_look_like_accessories가 True가 되어버려 이 테스트의
    전제("보정 검색이 필요 없다")와 충돌한다(2026-08-26 실측, _search_candidates에
    "relevant가 비면 원본 items로도 한 번 더 판정" 보정 로직을 추가하며 발견 -
    그 전엔 "버즈 3"(질의 "버즈3"와 띄어쓰기가 달라 token_set_ratio 75점으로
    _product_name_matches 자체가 False)라는 별개의 이유로 relevant가 우연히
    비어 있었던 덕에 이 테스트가 의도와 다른 경로로 우연히 통과하고 있었다)."""
    calls = []

    async def _fake_search(query, limit=5, sort_cd="A"):
        calls.append(sort_cd)
        return [_item("LG 그램 17 2026년형 코어 울트라 512GB 노트북", price=1890000)]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)

    result = asyncio.run(_search_candidates("LG 그램 17", base_query=None))

    assert calls == ["A"]
    assert len(result) == 1


def test_search_candidates_rescues_with_high_price_sort_when_all_relevant_look_like_accessories(monkeypatch):
    async def _fake_search(query, limit=5, sort_cd="A"):
        if sort_cd == "A":
            return [_item("마그세이프 폰 마운트 그립 홀더 아이폰 17 용", price=14400, code="acc")]
        return [_item("Apple 아이폰 17 프로 맥스 2TB 자급제", price=5500000, code="real")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)

    result = asyncio.run(_search_candidates("아이폰 17", base_query=None))

    codes = {it["product_code"] for it in result}
    assert codes == {"acc", "real"}


def test_search_candidates_dedupes_when_both_sorts_return_the_same_item(monkeypatch):
    async def _fake_search(query, limit=5, sort_cd="A"):
        return [_item("마그세이프 폰 마운트 그립 홀더 아이폰 17 용", price=14400, code="acc")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)

    result = asyncio.run(_search_candidates("아이폰 17", base_query=None))

    assert [it["product_code"] for it in result] == ["acc"]


def test_search_candidates_falls_back_to_empty_when_rescue_search_crashes(monkeypatch):
    """2026-08-26 실측(50개 질의 배치 - "아이패드 프로") - 11번가가 sortCd="H"
    보정 검색에 빈/깨진 XML을 돌려줘 xml.etree.ElementTree.ParseError로
    파이프라인 전체가 죽었다. 보정 검색이 실패해도 최소한 1차 검색 결과는
    그대로 써야 한다(전체가 죽으면 안 됨)."""
    import xml.etree.ElementTree as ET

    async def _fake_search(query, limit=5, sort_cd="A"):
        if sort_cd == "A":
            return [_item("마그세이프 폰 마운트 그립 홀더 아이폰 17 용", price=14400, code="acc")]
        raise ET.ParseError("no element found: line 1, column 0")

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)

    result = asyncio.run(_search_candidates("아이폰 17", base_query=None))

    assert [it["product_code"] for it in result] == ["acc"]


def test_search_candidates_falls_back_to_empty_when_primary_search_crashes(monkeypatch):
    import xml.etree.ElementTree as ET

    async def _boom(query, limit=5, sort_cd="A"):
        raise ET.ParseError("no element found: line 1, column 0")

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _boom)

    result = asyncio.run(_search_candidates("아이폰 17", base_query=None))

    assert result == []


def test_facet_resolved_true_when_option_already_in_query():
    assert _facet_resolved("메로나 빙그레", _facet("브랜드", ["빙그레", "롯데삼강"])) is True


def test_facet_resolved_false_when_no_option_in_query():
    assert _facet_resolved("메로나", _facet("브랜드", ["빙그레", "롯데삼강"])) is False


def test_strip_resolved_facets_removes_brand_already_in_query():
    """사용자가 이미 브랜드를 골라 재검색했는데, 이번 검색 결과에서도 여러
    브랜드가 다시 뽑히면(facet 추출은 매번 새로 하는 raw 추출이라 사용자가
    이미 고른 값을 모름) 프론트가 브랜드 선택 단계를 또 보여주는 버그가
    있었다 — 이미 질의에 반영된 facet은 목록에서 제거해야 한다."""
    facets = [_facet("브랜드", ["빙그레", "롯데삼강"]), _facet("용량", ["70ml", "200ml"])]

    stripped = _strip_resolved_facets("메로나 빙그레", facets)

    assert [f.label for f in stripped] == ["용량"]


def test_strip_resolved_facets_keeps_unresolved_facets():
    facets = [_facet("브랜드", ["빙그레", "롯데삼강"])]

    stripped = _strip_resolved_facets("메로나", facets)

    assert stripped == facets


def test_strip_cross_brand_options_removes_conflicting_brand():
    """2026-08-26, 사용자 리포트 - "아이폰 17 쳤는데 AI 상세검색에 갤럭시가
    왜 떠" - "아이폰/갤럭시 겸용 케이스" 같은 액세서리 상품명이 facet 추출
    표본에 섞이면 검색어와 무관한 브랜드가 옵션으로 나온다."""
    facets = [_facet("핸드폰 기종", ["아이폰 17 프로", "갤럭시 S26", "아이폰 17 프로 맥스"])]

    stripped = _strip_cross_brand_options("아이폰 17", facets)

    assert stripped[0].options == ["아이폰 17 프로", "아이폰 17 프로 맥스"]


def test_strip_cross_brand_options_drops_facet_when_fewer_than_two_remain():
    facets = [_facet("핸드폰 기종", ["아이폰 17 프로", "갤럭시 S26"])]

    stripped = _strip_cross_brand_options("아이폰 17", facets)

    assert stripped == []


def test_strip_cross_brand_options_keeps_facet_unchanged_when_no_conflict():
    facets = [_facet("용량", ["128GB", "256GB"])]

    stripped = _strip_cross_brand_options("아이폰 17", facets)

    assert stripped == facets


def test_is_ambiguous_facets_false_when_nothing_found():
    assert _is_ambiguous_facets("메로나", []) is False


def test_is_ambiguous_facets_false_when_single_option_each():
    facets = [_facet("브랜드", ["다이슨"]), _facet("용량", ["500ml"])]
    assert _is_ambiguous_facets("다이슨 청소기", facets) is False


def test_is_ambiguous_facets_true_when_multiple_options_not_yet_in_query():
    facets = [_facet("브랜드", ["다이슨", "삼성"])]
    assert _is_ambiguous_facets("무선청소기", facets) is True


def test_is_ambiguous_facets_false_when_option_already_chosen_in_query():
    """사용자가 이미 브랜드를 골라 검색어에 반영했으면(예: HITL 재검색), 검색
    결과가 여전히 여러 브랜드를 섞어 보여줘도 다시 묻지 않는다."""
    facets = [_facet("브랜드", ["다이슨", "삼성"])]
    assert _is_ambiguous_facets("무선청소기 다이슨", facets) is False


# -- 2026-08-16 하드닝: crossfilter가 옵션을 1개로 좁혔으면 되묻지 않는다 -----
# (그라운딩 회귀 파일럿 50개 중 발견: "햇반 백미 210g 24개"처럼 용량·수량을
# 이미 구체적으로 적었는데도, 브랜드 facet의 원본 옵션(CJ제일제당/시아스/하림)
# 중 어느 것도 질의에 문자 그대로 없다는 이유만으로 불필요하게 되물었다.
# options_by_selection은 "210g 24개"를 고르면 CJ제일제당 하나로 좁혀진다는
# 걸 이미 알고 있었는데 그 정보가 안 쓰이고 있었다 - 아래는 실제 라이브
# 파이프라인에서 관측된 값 그대로.)


def _brand_facet_with_crossfilter() -> ClarifyFacet:
    return ClarifyFacet(
        label="브랜드",
        options=["CJ제일제당", "시아스", "하림"],
        options_by_selection={
            "210g 24개": ["CJ제일제당"],
            "210g 12개": ["CJ제일제당"],
            "12개": ["CJ제일제당"],
            "48개": ["CJ제일제당"],
            "36개": ["CJ제일제당"],
            "8개": ["CJ제일제당", "시아스"],
        },
    )


def test_facet_resolved_true_when_crossfilter_narrows_to_single_option():
    assert _facet_resolved("햇반 백미 210g 24개", _brand_facet_with_crossfilter()) is True


def test_is_ambiguous_facets_false_when_crossfilter_narrows_to_single_option():
    assert _is_ambiguous_facets("햇반 백미 210g 24개", [_brand_facet_with_crossfilter()]) is False


def test_facet_resolved_false_when_crossfilter_selector_not_in_query():
    """셀렉터 키("210g 24개" 등)가 질의에 전혀 없으면(다른 용량/수량을 찾는
    질의라서) 원본 다중 옵션 그대로 애매함으로 남아야 한다 - 무조건 좁히는
    게 아니라 질의에 실제로 반영된 선택만 반영한다."""
    assert _facet_resolved("즉석밥 추천", _brand_facet_with_crossfilter()) is False


def test_facet_resolved_false_when_crossfilter_selectors_conflict_to_empty_intersection():
    """서로 다른 셀렉터 키 두 개가 동시에 질의에 매치되는데 교집합이 비면
    (모순되는 신호) 잘못 좁혀서 정말 필요한 되묻기를 건너뛰지 않고, 원본
    옵션 그대로 안전하게 남긴다."""
    facet = ClarifyFacet(
        label="브랜드",
        options=["A브랜드", "B브랜드"],
        options_by_selection={"12개": ["A브랜드"], "8개": ["B브랜드"]},
    )
    assert _facet_resolved("상품 12개 8개입", facet) is False


def test_resolved_facet_count_counts_resolved_facets():
    facets = [_facet("브랜드", ["해태제과"]), _facet("제품", ["초코파이 오리지널", "초코파이 다크"])]

    assert _resolved_facet_count("초코파이 해태제과", facets) == 1


def test_resolved_facet_count_ignores_unresolved_facets():
    facets = [_facet("브랜드", ["빙그레", "롯데삼강"])]

    assert _resolved_facet_count("메로나", facets) == 0



def test_build_facet_value_incidence_maps_values_to_matching_name_indices():
    facets = [_facet("브랜드", ["삼성전자", "APPLE"]), _facet("시리즈", ["갤럭시S25", "아이폰17"])]
    names = ["삼성전자 갤럭시S25 256GB", "APPLE 아이폰17 256GB"]

    incidence = _build_facet_value_incidence(facets, names)

    assert incidence["삼성전자"] == {0}
    assert incidence["apple"] == {1}
    assert incidence["갤럭시s25"] == {0}
    assert incidence["아이폰17"] == {1}


def test_build_facet_value_incidence_empty_set_for_value_with_no_match():
    facets = [_facet("브랜드", ["LG전자"])]
    names = ["삼성전자 갤럭시S25 256GB"]

    incidence = _build_facet_value_incidence(facets, names)

    assert incidence["lg전자"] == set()


def test_attach_facet_crossfilter_matches_existing_symmetric_scenario():
    """tests/test_clarify_facets.py의
    test_check_clarify_facets_attaches_facet_crossfilter_symmetrically와 같은
    시나리오를 check_clarify_facets 전체를 안 거치고 직접 검증 - incidence
    재구성이 기존 결과를 그대로 재현하는지 확인하는 더 빠른 회귀망."""
    facets = [
        _facet("브랜드", ["삼성전자", "APPLE"]),
        _facet("시리즈", ["갤럭시S25", "갤럭시Z 폴드8", "아이폰17", "아이폰17 프로"]),
    ]
    names = [
        "삼성전자 갤럭시S25 256GB",
        "삼성전자 갤럭시Z 폴드8 512GB",
        "APPLE 아이폰17 256GB",
        "APPLE 아이폰17 프로 512GB",
    ]

    updated = _attach_facet_crossfilter(facets, names)

    by_label = {f.label: f for f in updated}
    assert by_label["브랜드"].options_by_selection == {
        "갤럭시S25": ["삼성전자"],
        "갤럭시Z 폴드8": ["삼성전자"],
        "아이폰17": ["APPLE"],
        "아이폰17 프로": ["APPLE"],
    }
    assert by_label["시리즈"].options_by_selection == {
        "삼성전자": ["갤럭시S25", "갤럭시Z 폴드8"],
        "APPLE": ["아이폰17", "아이폰17 프로"],
    }


def test_facet_centrality_averages_option_degrees():
    incidence = {"삼성전자": {0, 1, 2}, "apple": {3}}
    facet = _facet("브랜드", ["삼성전자", "APPLE"])

    assert _facet_centrality(facet, incidence) == 2.0  # (3 + 1) / 2


def test_facet_centrality_zero_for_no_options():
    assert _facet_centrality(_facet("빈축", []), {}) == 0.0


def test_facet_sort_key_ignores_centrality_for_hint_matched_facet():
    """힌트가 잡는 facet("브랜드")은 incidence 내용과 무관하게 중심성을
    아예 안 본다 - 표본이 작을 때 중심성 신호가 불안정해질 위험으로부터
    안전해야 하는 케이스."""
    incidence = {"삼성전자": set(range(100)), "lg전자": set()}
    high_degree = _facet("브랜드", ["삼성전자"])
    low_degree = _facet("브랜드", ["LG전자"])

    assert _facet_sort_key(high_degree, incidence) == _facet_sort_key(low_degree, incidence)
    assert _facet_sort_key(high_degree, incidence)[1] == 0.0


def test_facet_sort_key_orders_hint_unmatched_facets_by_descending_centrality():
    """힌트가 못 잡는 facet들끼리는 incidence 중심성(평균 degree) 내림차순으로
    정렬돼야 한다 - LLM이 낸 임의 순서 대신."""
    incidence = {"넓은값": set(range(10)), "좁은값": {0}}
    broad = _facet("아무거나축", ["넓은값"])
    narrow = _facet("다른아무거나축", ["좁은값"])

    broad_key = _facet_sort_key(broad, incidence)
    narrow_key = _facet_sort_key(narrow, incidence)

    assert broad_key[0] == narrow_key[0] == len(_FACET_ORDER_HINTS)
    assert broad_key < narrow_key  # 중심성이 높을수록(더 넓은 축일수록) 먼저 온다
