"""한국 상품명에서 결정적 차이가 순한글 단어 하나뿐인 경우를 가려내는
배타 토큰 사전.

rapidfuzz의 token_set_ratio는 공통 토큰이 많으면 이런 차이를 그냥 덮어버린다
(실측: "햇반 백미 210g (24개)" vs "햇반 발아현미 210g (24개)" = 93.0점,
임계값 85를 가볍게 통과했다 - 임계값을 95로 올려도 여전히 통과한다).
유사도 점수로는 못 잡으므로 명시적 사전으로 막는다.

각 그룹의 원소는 서로 배타적이다(동시에 참일 수 없는 속성 값). 판정 규칙은
app.price_table의 모델명/수량 토큰 가드와 동일한 논리를 따른다:
- 양쪽 상품명에 모두 그 그룹의 원소가 등장할 때만 비교한다.
- 등장한 원소가 다르면 유사도 점수와 무관하게 불일치.
- 한쪽에만 등장하면 무시한다 - 쿼리가 축약된 것뿐일 수 있다
  ("햇반 210g 24개" vs "햇반 백미 210g 24개"는 일치로 봐야 한다).
"""

from __future__ import annotations

EXCLUSIVE_GROUPS: list[set[str]] = [
    {"백미", "현미", "발아현미", "흑미", "찹쌀", "잡곡"},
    {"순한맛", "매운맛", "얼큰한맛", "진한맛"},
    {"유선", "무선"},
    {"냉장", "냉동", "실온", "상온"},
    {"건면", "유탕면"},
    {"참기름", "들기름"},
    {"정품", "리퍼", "중고", "전시품"},
    {"국내산", "수입산", "미국산", "호주산"},
    {"무선청소기", "로봇청소기", "핸디청소기"},
    {"단품", "묶음", "세트"},
    # 2026-08-26, 사용자 리포트("아이폰 17 쳤는데 AI 상세검색에 갤럭시가 왜
    # 떠") - "아이폰/갤럭시 겸용 케이스"처럼 액세서리 상품명이 여러 폰 브랜드를
    # 한 상품명에 같이 나열하는 경우가 흔해, facet 추출 표본에 검색어와
    # 무관한 브랜드가 옵션으로 섞여 들어온다. app.debate._strip_cross_brand_
    # options가 이 그룹으로 "질의에 이미 언급된 브랜드와 배타적인 옵션"을
    # 걸러낸다.
    {"아이폰", "갤럭시", "샤오미", "화웨이"},
]


def _find_present_tokens(text: str, group: set[str]) -> set[str]:
    """긴 원소부터 먼저 찾고, 찾은 구간은 지워서 짧은 원소가 그 안에서 또
    걸리지 않게 한다 - 안 그러면 "발아현미" 안의 "현미"가 별도로도 걸려서
    같은 상품명인데 그룹 안에서 자기 자신과 충돌하는 것처럼 보일 수 있다."""
    remaining = text
    present: set[str] = set()
    for token in sorted(group, key=len, reverse=True):
        if token in remaining:
            present.add(token)
            remaining = remaining.replace(token, " ")
    return present


def exclusive_tokens_conflict(name_a: str, name_b: str) -> bool:
    """두 상품명 사이에 배타 토큰 충돌이 있으면 True - 유사도 점수가 아무리
    높아도 다른 상품으로 취급해야 한다는 뜻이다."""
    for group in EXCLUSIVE_GROUPS:
        present_a = _find_present_tokens(name_a, group)
        present_b = _find_present_tokens(name_b, group)
        if present_a and present_b and present_a.isdisjoint(present_b):
            return True
    return False
