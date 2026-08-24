"""app.price_table._product_name_matches(11번가 검색 결과 관련성 판정) 테스트.
전부 순수 함수 호출 - 네트워크 요청 없음."""

from __future__ import annotations

from app.price_table import _product_name_matches


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
