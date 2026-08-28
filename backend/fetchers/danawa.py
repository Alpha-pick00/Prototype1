"""다나와(search.danawa.com) 검색 결과 페이지를 스크래핑하는 보조 어댑터.

메인 검색 소스가 아니다 - app.adk_pipeline의 브랜드 우선순위 재정렬이
11번가 검색 결과 안에서 질의 브랜드와 일치하는 후보를 못 찾았을 때, "정말
그 브랜드 상품이 시중에 없는지, 아니면 11번가 표본에만 없는지"를 다나와에서
한 번 더 확인하는 보조 검증 수단으로만 쓴다(2026-08-28, 1000개 라이브
벤치마크 재조사 - 진짜 오매칭 92건 중 19건이 검색 API 표본 자체에 브랜드
상품이 없어서 생긴 회귀였다).

공식 API가 아니라 HTML 스크래핑이다 - 페이지 구조가 사전 공지 없이 바뀔 수
있고(과거 이 저장소에 있었던 fetchers/danawa_search.py가 그런 이유로
2026-08-20에 완전히 제거된 이력이 있다), 다나와가 접근을 막을 수도 있다.
그래서 이 모듈은 실패를 절대 예외로 던지지 않고 항상 빈 리스트로 조용히
처리한다(app.fetchers.elevenst와 동일한 계약) - 호출부가 이 함수 실패를
"검색 결과 없음"과 구분하지 않고 그대로 처리해도 파이프라인이 죽지 않는다.
"""

from __future__ import annotations

import logging
import re
from typing import TypedDict

import httpx
import lxml.html

logger = logging.getLogger(__name__)

SEARCH_URL = "https://search.danawa.com/dsearch.php"
REQUEST_TIMEOUT = 10.0

# 실측(2026-08-28) - User-Agent 없이 요청하면 다나와가 빈 결과나 다른 페이지를
# 돌려줄 수 있어 일반 브라우저로 위장한다. 이 값이 막히면(HTTP 오류/빈 결과)
# search_danawa가 그대로 빈 리스트를 반환하므로 파이프라인에는 영향이 없다.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# 상세페이지 링크는 두 형태가 섞여 있다(실측 2026-08-28) - 다나와 자체
# 상품 페이지는 pcode=, 최저가 비교(파트너샵 연결) 링크는 prod_id= 쿼리
# 파라미터를 쓴다. 둘 다 실제 상품을 가리키므로 어느 쪽이든 매치되면 된다.
_PROD_ID_RE = re.compile(r"[?&](?:prod_id|pcode)=(\d+)")
_PRICE_DIGITS_RE = re.compile(r"[^\d]")


class DanawaSearchItem(TypedDict):
    product_id: str
    product_name: str
    price_krw: int
    url: str
    image_url: str | None


def parse_search_html(html: str) -> list[DanawaSearchItem]:
    """네트워크 없이 순수하게 검색 결과 HTML만 파싱한다(fetchers.elevenst의
    parse_search_xml과 같은 패턴 - 테스트는 이 함수를 통해서 한다).

    검색 결과 목록(li.prod_item)에는 실제 상품 외에도 광고/프로모션 슬롯이
    섞여 있다(실측 - 44개 중 30개가 이름/가격 없는 빈 슬롯이거나 prod_id가
    없는 배너). 상세페이지 링크에 prod_id 쿼리 파라미터가 있는 것만 실제
    상품으로 취급한다."""
    tree = lxml.html.fromstring(html)
    items: list[DanawaSearchItem] = []
    for li in tree.xpath('//li[contains(@class, "prod_item")]'):
        name_links = li.xpath('.//p[@class="prod_name"]/a')
        price_els = li.xpath('.//p[contains(@class, "price_sect")]//strong')
        if not name_links or not price_els:
            continue

        href = name_links[0].get("href") or ""
        match = _PROD_ID_RE.search(href)
        if not match:
            continue
        product_id = match.group(1)

        product_name = name_links[0].text_content().strip()
        price_text = _PRICE_DIGITS_RE.sub("", price_els[0].text_content())
        if not product_name or not price_text:
            continue
        try:
            price_krw = int(price_text)
        except ValueError:
            continue

        image_els = li.xpath('.//div[contains(@class, "thumb_image")]//img')
        image_url = None
        if image_els:
            image_url = image_els[0].get("src") or image_els[0].get("data-original")
            if image_url and image_url.startswith("//"):
                image_url = "https:" + image_url

        items.append(
            DanawaSearchItem(
                product_id=product_id,
                product_name=product_name,
                price_krw=price_krw,
                url=href,
                image_url=image_url,
            )
        )
    return items


async def search_danawa(query: str, limit: int = 20) -> list[DanawaSearchItem]:
    """다나와 검색 결과 페이지를 스크래핑해 상품 목록을 돌려준다. 실패
    (타임아웃·HTTP 오류·파싱 실패·차단 응답으로 결과 0건)하면 예외를 던지지
    않고 빈 리스트를 돌려준다 - 이 모듈이 보조 검증 수단일 뿐이라, 실패해도
    호출부(app.adk_pipeline)는 기존 11번가 결과만으로 계속 진행할 수 있어야
    한다."""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=_HEADERS) as client:
            response = await client.get(SEARCH_URL, params={"query": query})
        if response.status_code != 200:
            return []
        items = parse_search_html(response.text)
        return items[:limit]
    except Exception:
        logger.exception("danawa 검색 crashed for query=%r", query)
        return []
