"""11번가 오픈 API(openapi.11st.co.kr, ProductSearch)로 상품을 검색하는 어댑터.

다나와(fetchers/danawa*.py)와 달리 HTML 스크래핑이 아니라 11번가가 공식
제공하는 구조화 XML API를 호출한다 - 페이지 구조가 사이트마다 달라 스니펫
파싱이 어긋나는 문제 자체가 없다.

실측(2026-08-20)으로 확인한 함정: 응답이 `encoding="EUC-KR"`로 온다(공식
가이드 문서에는 명시돼 있지 않음). 요청 키워드는 평범한 UTF-8 URL 인코딩으로
보내도 검색 자체는 정상 동작하지만(httpx가 자동으로 처리), 응답 바이트를
UTF-8로 디코딩하면 ProductName 등 모든 한글 필드가 깨진다 - 반드시
`response.content`를 `"euc-kr"`로 명시적으로 디코딩한 뒤 XML 파싱해야 한다.
"""

from __future__ import annotations

import logging
from typing import TypedDict
from xml.etree import ElementTree

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

API_URL = "http://openapi.11st.co.kr/openapi/OpenApiService.tmall"
REQUEST_TIMEOUT = 10.0

# 실측(2026-08-20, apiCode=ProductSearch&option=Categories)으로 확인한 함정 -
# 11번가는 다나와식 대분류/중분류 상품 카테고리 색인이 아니라, 검색어와 매칭된
# "전시 카테고리"(dispCtgr) 목록을 평면으로 돌려준다. 여기엔 실제 상품
# 카테고리(예: "과자/간식")와 프로모션 탭/이벤트 전시관(예: "홈쇼핑 Tab",
# "9900원샵", "2020설", "쿠폰 발급용 추가 전시 카테고리")이 구분 없이 섞여
# 있고, 건수(CategoryPrdCnt)도 실제 상품 수가 아니라 그 전시관의 진열 슬롯
# 수라 조작된 상품 카테고리보다 더 높게 나오는 경우가 흔하다(실측: "오리온
# 초코파이 말차쇼콜라" 검색 시 "과자/간식"(117건, 실제 카테고리)보다 "홈쇼핑
# Tab"/"9900원샵"(각 120건, 프로모션 전시관)이 더 위에 옴). facet으로 그대로
# 노출하면 사용자에게 "카테고리: 홈쇼핑 Tab" 같은 무의미한 선택지가 뜨므로,
class ElevenstSearchBlocked(RuntimeError):
    """API가 에러 코드로 응답했을 때만 던진다(예: 003 미등록 키) - 그 외
    실패(타임아웃, 파싱 실패, 결과 없음)는 빈 리스트로 조용히 처리한다는
    계약을 유지한다. fetchers.danawa_search.DanawaSearchBlocked와 같은 이유
    (배치 호출자가 "진짜로 상품이 없다"와 "키/설정이 잘못됐다"를 구분해야
    안전하게 멈출 수 있다)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"11번가 오픈 API 오류 (code={code}): {message}")
        self.code = code


class ElevenstSearchItem(TypedDict):
    product_code: str
    product_name: str
    price_krw: int
    seller: str
    url: str
    review_count: int | None
    buy_satisfy: int | None
    image_url: str | None


def _text(el: ElementTree.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_search_xml(xml_text: str) -> list[ElevenstSearchItem]:
    """네트워크 없이 순수하게 XML 응답만 파싱한다(fetchers.danawa_search의
    parse_search_html과 같은 패턴 - 테스트는 전부 이 함수를 통해서 한다).
    호출부가 이미 EUC-KR로 디코딩한 str을 넘겨준다는 전제."""
    root = ElementTree.fromstring(xml_text)

    error = root.find("Error")
    if error is not None:
        code = _text(error.find("Code"))
        message = _text(error.find("Message"))
        raise ElevenstSearchBlocked(code, message)

    items: list[ElevenstSearchItem] = []
    for product in root.findall(".//Product"):
        product_code = _text(product.find("ProductCode"))
        product_name = _text(product.find("ProductName"))
        url = _text(product.find("DetailPageUrl"))
        if not product_code or not product_name or not url:
            continue
        # SalePrice(할인 반영 실판매가)를 우선 쓰고, 없으면 ProductPrice(정가)로
        # 대체한다 - Benefit/Discount가 없는 상품은 SalePrice가 비어있을 수 있다.
        price_text = _text(product.find("SalePrice")) or _text(product.find("ProductPrice"))
        try:
            price_krw = int(price_text)
        except ValueError:
            continue
        review_text = _text(product.find("ReviewCount"))
        satisfy_text = _text(product.find("BuySatisfy"))
        # ProductImage300(가로 300px) - 실측(2026-08-24)으로 확인한 카드 UI에 쓸
        # 썸네일 크기. 100~300 사이 여러 크기 태그가 오는데(11dims CDN 리사이즈
        # 파라미터만 다름) 카드에 맞는 해상도 하나만 쓴다 - 없으면(드묾) 원본
        # ProductImage로 대체.
        image_url = _text(product.find("ProductImage300")) or _text(product.find("ProductImage")) or None
        items.append(
            ElevenstSearchItem(
                product_code=product_code,
                product_name=product_name,
                price_krw=price_krw,
                seller=_text(product.find("SellerNick")) or _text(product.find("Seller")) or "11번가",
                url=url,
                review_count=int(review_text) if review_text.isdigit() else None,
                buy_satisfy=int(satisfy_text) if satisfy_text.isdigit() else None,
                image_url=image_url,
            )
        )
    return items


WEB_RANKING_API_URL = "https://apis.11st.co.kr/search/api/tab"


def _parse_web_ranking_json(data: dict) -> list[ElevenstSearchItem]:
    """네트워크 없이 순수하게 웹 랭킹 API(WEB_RANKING_API_URL) 응답 JSON만
    파싱한다(parse_search_xml과 같은 패턴 - 테스트는 이 함수를 통해서 한다).
    응답 최상위 "data"는 여러 위젯 섹션이 평면 배열로 섞여 있다(연관검색어,
    광고, 애플/삼성 공식관, 최저가 모음 등) - groupName == "list"인 섹션만
    실제 검색 결과 목록이고, 나머지는 전부 프로모션/광고 위젯이라 무시한다."""
    items: list[ElevenstSearchItem] = []
    for section in data.get("data") or []:
        if not isinstance(section, dict) or section.get("groupName") != "list":
            continue
        for raw in section.get("items") or []:
            product_code = str(raw.get("id") or "")
            product_name = str(raw.get("title") or "")
            url = str(raw.get("linkUrl") or "")
            price = raw.get("finalPrc")
            if not product_code or not product_name or not url or not isinstance(price, int):
                continue
            review_text = str(raw.get("reviewCountText") or "").replace(",", "")
            satisfy = raw.get("satisfactionScore")
            items.append(
                ElevenstSearchItem(
                    product_code=product_code,
                    product_name=product_name,
                    price_krw=price,
                    seller=str(raw.get("sellerNickName") or "") or "11번가",
                    url=url,
                    review_count=int(review_text) if review_text.isdigit() else None,
                    buy_satisfy=int(satisfy) if isinstance(satisfy, (int, float)) else None,
                    image_url=str(raw.get("imageUrl") or "") or None,
                )
            )
    return items


async def search_elevenst_web_ranking(query: str, limit: int = 30) -> list[ElevenstSearchItem]:
    """11번가 웹사이트(search.11st.co.kr)가 실제로 쓰는 비공식 내부 API를
    직접 호출한다(2026-08-27, 사용자 리포트 - "11번가에 아이폰 17 검색하면
    핸드폰으로 제대로 매핑되는데 왜 오픈 API 통해서 하면 액세서리가 뜨는거야"
    -> "웹사이트에서 먼저 추천도순 가져오고 API에서 매핑해주면 되잖아").

    실측(2026-08-27)으로 확인한 사실 - 공식 오픈 API(search_elevenst,
    ProductSearch)의 sortCd="A"(추천도순 문서상 의미)는 실제 11st.co.kr
    웹사이트가 사람에게 보여주는 순위와 다른 알고리즘이라, 인기·고가
    상품(아이폰/맥북 등)에서 액세서리가 상단을 차지하는 경우가 흔하다.
    이 함수가 호출하는 apis.11st.co.kr/search/api/tab은 웹사이트 검색
    페이지(search.11st.co.kr/pc/total-search)가 브라우저에서 직접 호출하는
    바로 그 API다(Playwright로 네트워크 캡처해 확인) - 인증키·쿠키·세션
    없이 순수 GET 한 번으로 웹사이트와 동일한 순서(본품이 상단)를 그대로
    받는다.

    비공식 API라는 한계를 명시한다 - 11번가가 공식 문서화하지 않았으므로
    사전 공지 없이 응답 스키마가 바뀌거나 접근이 막힐 수 있다. 그런
    상황에서도 파이프라인 전체가 죽지 않도록, 실패(타임아웃·HTTP 오류·JSON
    파싱 실패·스키마 변경으로 items 0건)하면 예외를 던지지 않고 빈
    리스트를 돌려준다 - 호출부(app.adk_pipeline)가 이 함수 실패를 "검색
    결과 없음"과 구분하지 않고 그대로 처리해도 안전하다."""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(
                WEB_RANKING_API_URL,
                params={"kwd": query, "tabId": "TOTAL_SEARCH"},
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
    except Exception:
        logger.exception("11번가 웹 랭킹 API 호출 실패 for query=%r", query)
        return []

    try:
        return _parse_web_ranking_json(data)[:limit]
    except Exception:
        logger.exception("11번가 웹 랭킹 API 응답 파싱 실패 for query=%r", query)
        return []


async def search_elevenst(query: str, limit: int = 5, sort_cd: str = "A") -> list[ElevenstSearchItem]:
    """11번가 ProductSearch API를 호출해 상품을 찾는다. sortCd 기본값 "A"는
    "가격 오름차순"이 아니라 "추천도순"이다(2026-08-26 실측 정정 - 이전
    docstring이 틀렸었다: 7개 정렬 코드를 전부 라이브로 찍어본 결과 A는
    가격순이 아니었고, 진짜 낮은가격순(L)은 오히려 "10원"짜리 가격비교
    중지/미끼가 상품만 나왔다). 인기·고가 상품(예: "아이폰 17")은 추천도순
    표본 안에 실제 본품이 아예 안 잡히고 표기에 상품명을 끼워 넣은 저가
    액세서리만 잡히는 경우가 있는데, 이때 "H"(높은가격순)로 다시 찾으면
    본품이 나온다(실측: H로 아이폰 17 프로맥스 550만원대 매물 확인) -
    price_table.most_candidates_look_like_accessories가 이 경우를 감지해
    _search_candidates가 그때만 sort_cd="H"로 보정 검색을 추가로 태운다.
    키가 없으면(.env 미설정) 즉시 빈 리스트 - 호출부가 "설정 안 됨"과
    "검색 결과 없음"을 굳이 구분할 필요가 없는 초기 단계라 조용히 넘어간다."""
    if not settings.elevenst_api_key:
        return []

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(
            API_URL,
            params={
                "key": settings.elevenst_api_key,
                "apiCode": "ProductSearch",
                "keyword": query,
                "pageNum": 1,
                "pageSize": limit,
                "sortCd": sort_cd,
            },
        )
        response.raise_for_status()
        xml_text = response.content.decode("euc-kr")

    return parse_search_xml(xml_text)[:limit]
