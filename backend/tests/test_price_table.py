"""app.price_table._product_name_matches(11번가 검색 결과 관련성 판정) 및
semantic_relevance_fallback(임베딩 기반 2차 구제) 테스트. 전부 순수 함수/
monkeypatch 호출 - 네트워크 요청 없음."""

from __future__ import annotations

import asyncio

from app import price_table
from app.price_table import _product_name_matches
from fetchers.elevenst import ElevenstSearchItem


def _item(name: str, code: str = "1", price: int = 1000) -> ElevenstSearchItem:
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


def test_product_name_matches_true_for_genuine_match():
    assert _product_name_matches("아이간식", "창억떡 꿀설기 아이간식 500g") is True


def test_product_name_matches_false_for_keyword_stuffed_unrelated_product():
    """실측(2026-08-24) - "아이간식" 검색 시 "자동 제면기 파스타 기계
    식사준비 건강식 아이간식 식당 업소용 가정용 뽑는 조리"가 관련 상품으로
    통과했다. 상품명에 질의 토큰이 부분집합으로 들어있기만 하면
    token_set_ratio는 100점을 주지만, 실제로는 무관한 상품(파스타 기계)이다."""
    assert (
        _product_name_matches(
            "아이간식", "자동 제면기 파스타 기계 식사준비 건강식 아이간식 식당 업소용 가정용 뽑는 조리"
        )
        is False
    )


def test_product_name_matches_true_for_short_query_against_long_legitimate_title():
    """회귀 방지 - "이프로"처럼 검색어 자체가 짧고 후보 상품명이 정상적으로
    긴 경우(도배가 아니라 그냥 상세한 상품명)까지 걸러내면 안 된다. 실측
    token_sort_ratio가 19.99점으로 임계값 20 기준이면 오탈락했었다."""
    assert _product_name_matches("이프로", "이프로 부족할때 제로 복숭아 500ml x 24개") is True


def test_semantic_relevance_fallback_rescues_reworded_compound_word(monkeypatch):
    """실측(2026-08-24, 사용자 리포트 "망고주스를 사고 싶어 검색이 안 됨") -
    "망고주스"(붙여쓰기)는 11번가 실제 표기 "오렌지망고 ... 주스"(쪼개지고
    순서도 다름)와 토큰이 하나도 안 겹쳐 _product_name_matches가 전부
    거부한다(rapidfuzz token_set_ratio 19.5점). 임베딩 유사도는 표기 차이와
    무관하게 이런 진짜 매치를 구제해야 한다."""

    async def _fake_embed(texts):
        if texts == ["망고주스"]:
            return [[1.0, 0.0]]
        return [[0.9, 0.1]]  # 후보 벡터 - 코사인 유사도 0.99, 임계값 0.6 통과

    monkeypatch.setattr(price_table.embeddings, "embed", _fake_embed)

    items = [_item("카프리썬 오렌지망고 200ml x 40입 주스")]
    result = asyncio.run(price_table.semantic_relevance_fallback("망고주스", items))

    assert [it["product_code"] for it in result] == ["1"]


def test_semantic_relevance_fallback_rejects_below_threshold(monkeypatch):
    async def _fake_embed(texts):
        if texts == ["망고주스"]:
            return [[1.0, 0.0]]
        return [[0.0, 1.0]]  # 코사인 유사도 0.0 - 임계값 0.6 미달

    monkeypatch.setattr(price_table.embeddings, "embed", _fake_embed)

    items = [_item("무관한 상품")]
    result = asyncio.run(price_table.semantic_relevance_fallback("망고주스", items))

    assert result == []


def test_semantic_relevance_fallback_still_rejects_model_conflict_despite_high_similarity(monkeypatch):
    """의미 유사도만으로는 모델 세대 차이(아이폰6 vs 아이폰15)를 못 잡으므로
    model_or_quantity_conflict 가드는 임베딩 유사도가 높아도 그대로 적용돼야
    한다."""

    async def _fake_embed(texts):
        return [[1.0, 0.0]]

    monkeypatch.setattr(price_table.embeddings, "embed", _fake_embed)

    items = [_item("아이폰6 케이스")]
    result = asyncio.run(price_table.semantic_relevance_fallback("아이폰15 케이스", items))

    assert result == []


def test_semantic_relevance_fallback_returns_empty_when_embedding_unavailable(monkeypatch):
    async def _no_embed(texts):
        return None

    monkeypatch.setattr(price_table.embeddings, "embed", _no_embed)

    items = [_item("망고주스 진짜 매치")]
    result = asyncio.run(price_table.semantic_relevance_fallback("망고주스", items))

    assert result == []


def test_semantic_relevance_fallback_returns_empty_for_no_items():
    assert asyncio.run(price_table.semantic_relevance_fallback("망고주스", [])) == []


# ---------------------------------------------------------------------------
# all_candidates_look_like_accessories / _dedupe_by_product_code (2026-08-26,
# "아이폰 17" 실측 - 추천도순 표본이 전부 액세서리였던 문제의 로컬 트리거)
# ---------------------------------------------------------------------------


def test_all_candidates_look_like_accessories_true_when_every_item_is_accessory():
    items = [
        _item("마그세이프 폰 마운트 그립 홀더 아이폰 17 용"),
        _item("베이스어스 핑거 링 홀더 아이폰 17 에어"),
    ]
    assert price_table.all_candidates_look_like_accessories("아이폰 17", items) is True


def test_all_candidates_look_like_accessories_false_when_one_item_is_not():
    items = [
        _item("마그세이프 폰 마운트 그립 홀더 아이폰 17 용"),
        _item("Apple 아이폰 17 프로 맥스 2TB 자급제"),
    ]
    assert price_table.all_candidates_look_like_accessories("아이폰 17", items) is False


def test_all_candidates_look_like_accessories_false_for_empty_list():
    assert price_table.all_candidates_look_like_accessories("아이폰 17", []) is False


def test_all_candidates_look_like_accessories_false_when_query_itself_wants_accessory():
    """"아이폰 케이스"를 검색했으면 결과가 전부 케이스인 게 정상이다 - 이때는
    보정 검색을 태우면 안 된다."""
    items = [_item("아이폰 17 실리콘 케이스")]
    assert price_table.all_candidates_look_like_accessories("아이폰 케이스", items) is False


def test_dedupe_by_product_code_removes_duplicates_keeping_first():
    items = [_item("A", code="1"), _item("A 중복", code="1"), _item("B", code="2")]
    deduped = price_table._dedupe_by_product_code(items)
    assert [it["product_code"] for it in deduped] == ["1", "2"]
    assert deduped[0]["product_name"] == "A"


# ---------------------------------------------------------------------------
# filter_out_accessory_noise / filter_price_outliers (2026-08-26, "아이폰
# 16/17" 데모 검증 - sortCd=H 보정으로 본품을 찾아와도 액세서리가 후보 풀에
# 그대로 남거나 이상치 가격 매물이 섞이는 문제)
# ---------------------------------------------------------------------------


def test_filter_out_accessory_noise_removes_accessories_when_query_wants_the_phone():
    items = [
        _item("내셔널지오그래픽 슬림한 디자인 아이폰 16 프로 클리어케이스", code="1"),
        _item("Apple 아이폰 16 프로 512GB [자급제] 화이트티타늄", code="2"),
    ]
    result = price_table.filter_out_accessory_noise("아이폰 16", items)
    assert [it["product_code"] for it in result] == ["2"]


def test_filter_out_accessory_noise_keeps_accessories_when_query_wants_accessory():
    items = [_item("아이폰 17 실리콘 케이스", code="1")]
    result = price_table.filter_out_accessory_noise("아이폰 케이스", items)
    assert [it["product_code"] for it in result] == ["1"]


def test_filter_out_accessory_noise_falls_back_to_original_when_all_are_accessories():
    """전부 액세서리뿐이면(단어 목록이 본품도 잘못 잡았거나, 진짜 액세서리
    밖에 없는 경우) 하드 필터로 다 날리지 않고 원래 목록을 그대로 둔다."""
    items = [_item("아이폰 16 케이스", code="1")]
    result = price_table.filter_out_accessory_noise("아이폰 16", items)
    assert [it["product_code"] for it in result] == ["1"]


def test_filter_price_outliers_removes_absurd_price_real_measurement():
    """실측(2026-08-26) - "아이폰 16" 관련성 필터 통과 매물 18개 중 16개는
    388만~500만원대, 2개만 2,515만원/2,998만원(정상가 대비 5~7배)로 튀었다."""
    normal_prices = [
        3888700, 3888700, 3897280, 3900000, 3935000, 3972930, 4100000, 4200000,
        4288880, 4300000, 4524730, 4524730, 4915750, 4915750, 4915750, 5000000,
    ]
    items = [_item(f"Apple 아이폰 16 프로 {p}", code=str(i), price=p) for i, p in enumerate(normal_prices)]
    items.append(_item("Apple 아이폰 16 프로 맥스 1TB 이상치1", code="outlier1", price=25150000))
    items.append(_item("Apple 아이폰 16 프로 맥스 1TB 이상치2", code="outlier2", price=29980000))

    result = price_table.filter_price_outliers(items)

    assert {it["product_code"] for it in result} == {str(i) for i in range(len(normal_prices))}


def test_filter_price_outliers_keeps_tight_cluster_real_measurement():
    """실측(2026-08-26) - "아이폰 17"은 22개 전부 399만~552만원 사이로
    이상치가 없었다 - 이 경우 아무것도 제외되면 안 된다."""
    prices = [
        3990000, 3990000, 3990000, 3990000, 3990000, 3990000, 3990000, 3990000,
        3990000, 4021500, 4021500, 4021900, 4055400, 4169800, 4283000, 4283000,
        4283000, 4487300, 5500000, 5500000, 5500000, 5525200,
    ]
    items = [_item(f"애플 아이폰 17 프로 맥스 {p}", code=str(i), price=p) for i, p in enumerate(prices)]

    result = price_table.filter_price_outliers(items)

    assert len(result) == len(items)


def test_filter_price_outliers_skips_small_samples():
    """표본이 3개 미만이면 중앙값 판단 자체가 불안정하므로 건너뛴다."""
    items = [_item("A", code="1", price=1000), _item("B", code="2", price=999999)]
    result = price_table.filter_price_outliers(items)
    assert len(result) == 2


def test_filter_price_outliers_falls_back_when_everything_is_an_outlier():
    items = [
        _item("A", code="1", price=1),
        _item("B", code="2", price=1),
        _item("C", code="3", price=100000000),
    ]
    result = price_table.filter_price_outliers(items)
    assert len(result) >= 1


# ---------------------------------------------------------------------------
# all_candidates_look_like_used_condition (2026-08-26, "아이폰 16" AI
# 상세검색 - 액세서리 제거 후 남은 표본이 전부 중고폰 매물이라 "용량" facet
# 값에서 "GB" 단위가 빠졌던 문제)
# ---------------------------------------------------------------------------


def test_all_candidates_look_like_used_condition_true_when_every_item_is_used():
    items = [
        _item("S등급 아이폰 16 프로 256 블랙 티타늄 중고폰 공기계"),
        _item("A등급 아이폰 16 128 화이트 중고폰 공기계"),
    ]
    assert price_table.all_candidates_look_like_used_condition("아이폰 16", items) is True


def test_all_candidates_look_like_used_condition_false_when_one_is_new():
    items = [
        _item("S등급 아이폰 16 프로 256 블랙 티타늄 중고폰 공기계"),
        _item("Apple 아이폰 16 프로 512GB [자급제]"),
    ]
    assert price_table.all_candidates_look_like_used_condition("아이폰 16", items) is False


def test_all_candidates_look_like_used_condition_false_for_empty_list():
    assert price_table.all_candidates_look_like_used_condition("아이폰 16", []) is False


def test_all_candidates_look_like_used_condition_false_when_query_itself_wants_used():
    items = [_item("S등급 아이폰 16 프로 256 중고폰 공기계")]
    assert price_table.all_candidates_look_like_used_condition("아이폰 16 중고", items) is False
