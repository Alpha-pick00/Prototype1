"""에이전트별 후보 배열을 병합해 동일 상품을 하나의 후보로 합친다.

동일 상품 판별 우선순위:
1. URL 정규화 후 완전 일치 — 단, 가격이 서로 호환되는 범위일 때만 병합한다.
   가격이 크게 다르면 같은 URL이라도 "상품 상세 페이지가 아니라 여러 상품이
   걸리는 목록/전시 페이지"라는 신호로 보고 별개 후보로 남긴다.
2. (판매처, 가격) 완전 일치 — 단, 상품명이 아예 무관하지 않을 때만 병합한다.
   (같은 판매처에서 우연히 같은 가격인 다른 상품이 섞이는 걸 막는다)
3. 상품명 유사도(rapidfuzz token_set_ratio >= NAME_SIMILARITY_THRESHOLD)

2·3번(상품명 유사도 기반 병합)은 유사도 임계값을 넘어도 모델/규격/수량/구매유형
토큰이 충돌하면(_different_product) 병합하지 않는다 - "아이폰6 케이스"와
"아이폰15 케이스"처럼 공통 토큰이 많아 유사도 점수만으로는 다른 상품임을 못
가리는 경우를 막는다(app.price_table의 다나와 매칭에 이미 있던 가드와 동일).
"""

from __future__ import annotations

from collections import Counter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rapidfuzz import fuzz

from app.exclusive_tokens import exclusive_tokens_conflict
from app.schemas import AgentCandidate
from app.spec_match import model_or_quantity_conflict

# utm_*만 순수 광고 추적용으로 보고 제거한다. spec·itemId처럼 상품/옵션 자체를
# 가리키는 파라미터까지 지우면 서로 다른 상품이 같은 URL로 오인 합쳐질 수 있어
# 보수적으로 접근한다 — 다른 트래커를 추가로 지워야 하면 이 튜플만 늘리면 된다.
_TRACKING_PREFIXES = ("utm_",)

NAME_SIMILARITY_THRESHOLD = 85
# (판매처, 가격) 일치만으로 병합하면 같은 판매처의 다른 상품이 섞일 수 있어
# 상품명이 최소한 무관하지는 않은지 확인하는 낮은 문턱값의 가드.
SELLER_PRICE_NAME_GUARD = 60
# 쿠폰/배송비 정도의 차이만 허용한다. 이보다 크게 벌어지면 같은 URL이라도
# 다른 상품으로 취급한다.
PRICE_COMPAT_TOLERANCE = 0.05


def _different_product(name_a: str, name_b: str) -> bool:
    """상품명 유사도만으로 병합할 때, 모델·규격·수량·구매유형(정품/리퍼/중고 등)
    토큰이 충돌하면 유사도 점수와 무관하게 다른 상품으로 취급한다(app.price_table의
    다나와 실측가 매칭에 이미 있던 가드를 여기로도 적용 - 사용자 리포트,
    2026-08-14: "핸드폰 케이스" 검색에서 옛날 모델이 섞여 나옴. "아이폰6 케이스"
    vs "아이폰15 케이스"는 공통 토큰("아이폰","케이스")이 많아 token_set_ratio가
    임계값을 넘어 같은 그룹으로 합쳐지고, 그 그룹의 대표는 최저가 멤버라 구형
    모델이 대표로 뽑힐 수 있었다)."""
    if model_or_quantity_conflict(name_a, name_b):
        return True
    if exclusive_tokens_conflict(name_a, name_b):
        return True
    return False


def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            urlencode(query_pairs),
            "",
        )
    )


def _prices_compatible(p1: int | None, p2: int | None) -> bool:
    """한쪽이라도 가격을 모르면 병합을 막을 근거가 없으므로 호환으로 본다.
    둘 다 알면 5% 이내 차이(쿠폰/배송비 수준)만 호환으로 본다."""
    if p1 is None or p2 is None:
        return True
    if p1 == p2:
        return True
    return abs(p1 - p2) / max(p1, p2) <= PRICE_COMPAT_TOLERANCE


def _majority(values: list, canonical_value):
    """가장 많은 표를 받은 값을 반환한다. 동률이면 canonical_value(대표 후보의 값)를 우선한다."""
    counter = Counter(values)
    max_count = max(counter.values())
    tied = [v for v, c in counter.items() if c == max_count]
    if len(tied) == 1:
        return tied[0]
    return canonical_value if canonical_value in tied else tied[0]


class _Group:
    def __init__(self, agent: str, candidate: AgentCandidate) -> None:
        self.members: list[tuple[str, AgentCandidate]] = [(agent, candidate)]
        self.norm_url = normalize_url(candidate.url)

    def add(self, agent: str, candidate: AgentCandidate) -> None:
        self.members.append((agent, candidate))
        if not self.norm_url:
            self.norm_url = normalize_url(candidate.url)

    def matches(self, candidate: AgentCandidate) -> bool:
        norm_url = normalize_url(candidate.url)
        if norm_url and self.norm_url and norm_url == self.norm_url:
            if all(_prices_compatible(candidate.price_krw, m.price_krw) for _, m in self.members):
                return True
        for _, member in self.members:
            if (
                candidate.price_krw is not None
                and member.price_krw == candidate.price_krw
                and candidate.retailer
                and (candidate.retailer or "") == (member.retailer or "")
                and fuzz.token_set_ratio(candidate.product_name, member.product_name)
                >= SELLER_PRICE_NAME_GUARD
                and not _different_product(candidate.product_name, member.product_name)
            ):
                return True
        for _, member in self.members:
            if (
                fuzz.token_set_ratio(candidate.product_name, member.product_name)
                >= NAME_SIMILARITY_THRESHOLD
                and not _different_product(candidate.product_name, member.product_name)
            ):
                return True
        return False

    def to_dict(self, shared_url_counts: dict[str, int]) -> dict:
        # URL이 가장 짧은(=파라미터가 적은) 멤버를 대표 후보로 삼아 상품명 동률 시 근거로 쓴다.
        canonical = min(self.members, key=lambda m: len(m[1].url or ""))[1]

        product_name = _majority(
            [m.product_name for _, m in self.members], canonical.product_name
        )

        # 가격/판매처/URL은 한 세트로 같은 멤버에서 가져온다 — 서비스의 핵심 가치가
        # "동일 상품의 최저가 안내"이므로, 그룹 내 최저가를 제시한 멤버를 그대로
        # 대표로 삼는다(가격만 최저가로 바꾸고 URL은 다수결로 뽑으면 그 최저가를
        # 실제로 안 파는 판매처의 링크가 붙는 불일치가 생긴다).
        priced_members = [m for _, m in self.members if m.price_krw is not None]
        cheapest = min(priced_members, key=lambda m: m.price_krw) if priced_members else canonical

        price_krw = cheapest.price_krw
        retailer = cheapest.retailer
        url = cheapest.url
        # 이미지는 다나와 출신 멤버만 갖고 있다(다른 에이전트는 LLM이 지어내지
        # 않도록 애초에 안 채움) - cheapest에 없으면 그룹 내 다른 멤버에서라도 가져온다.
        image_url = cheapest.image_url or next(
            (m.image_url for _, m in self.members if m.image_url), None
        )

        member_norm_urls = {normalize_url(m.url) for _, m in self.members if m.url}
        conflict_counts = [
            shared_url_counts[u] for u in member_norm_urls if shared_url_counts.get(u, 0) >= 2
        ]

        return {
            "product_name": product_name,
            "price_krw": price_krw,
            "url": url,
            "retailer": retailer,
            "image_url": image_url,
            "reasons": [m.reasoning for _, m in self.members if m.reasoning],
            "proposed_by": [a for a, _ in self.members],
            "signals": {},
            "final_score": 0.0,
            "flags": {
                "shared_url": bool(conflict_counts),
                "shared_url_count": max(conflict_counts) if conflict_counts else 0,
            },
        }


def _shared_url_counts(entries: list[tuple[str, AgentCandidate]]) -> dict[str, int]:
    """정규화된 URL별로 후보를 모아, 그중 가격이 유의하게 다른 쌍이 하나라도
    있으면 "공유 URL"(상품 상세가 아니라 목록/전시 페이지일 가능성)로 표시한다."""
    by_url: dict[str, list[AgentCandidate]] = {}
    for _, candidate in entries:
        norm = normalize_url(candidate.url)
        if not norm:
            continue
        by_url.setdefault(norm, []).append(candidate)

    conflicted: dict[str, int] = {}
    for url, members in by_url.items():
        if len(members) < 2:
            continue
        has_conflict = any(
            not _prices_compatible(a.price_krw, b.price_krw)
            for i, a in enumerate(members)
            for b in members[i + 1 :]
        )
        if has_conflict:
            conflicted[url] = len(members)
    return conflicted


def merge_candidates(entries: list[tuple[str, AgentCandidate]]) -> list[dict]:
    """entries: [(agent_name, candidate), ...]. 이미 유효성 필터링된 후보만 들어온다고 가정한다."""
    groups: list[_Group] = []
    for agent, candidate in entries:
        target = next((g for g in groups if g.matches(candidate)), None)
        if target is None:
            groups.append(_Group(agent, candidate))
        else:
            target.add(agent, candidate)

    shared_url_counts = _shared_url_counts(entries)
    return [group.to_dict(shared_url_counts) for group in groups]
