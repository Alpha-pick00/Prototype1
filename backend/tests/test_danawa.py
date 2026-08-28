"""fetchers.danawa(다나와 검색 결과 페이지 스크래핑) 테스트.
네트워크 요청 금지 - parse_search_html은 순수 함수로, search_danawa는
httpx.MockTransport로 테스트한다."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from fetchers import danawa

# 실측(2026-08-28) HTML 구조를 그대로 축약한 fixture - 다나와 자체 상품 링크
# (pcode=)와 최저가 비교 링크(prod_id=) 두 형태가 섞여 있고, 이름/가격이 없는
# 광고 슬롯도 함께 온다(실측: 44개 중 30개가 광고/빈 슬롯).
_SAMPLE_HTML = """
<html><body>
<ul class="product_list">
  <li class="prod_item width_change searched">
    <div class="thumb_image"><img src="//gdimg.gmarket.co.kr/1/goodsimage.jpg" /></div>
    <p class="prod_name">
      <a href="https://prod.danawa.com/bridge/go_link_goods.php?prod_id=1001&keyword=x">
        콜러노비타 리모컨 방수비데 BD-SD600
      </a>
    </p>
    <p class="price_sect"><strong>184,560</strong></p>
  </li>
  <li class="prod_item width_change searched">
    <div class="thumb_image"><img src="https://img.danawa.com/2/goods.webp" /></div>
    <p class="prod_name">
      <a href="https://prod.danawa.com/info/?pcode=2002&keyword=x">
        스노우피크 화로대SR 해외구매
      </a>
    </p>
    <p class="price_sect"><strong>127,700</strong></p>
  </li>
  <li class="prod_item width_change ad_slot">
    <div class="thumb_image"></div>
  </li>
  <li class="prod_item width_change searched">
    <p class="prod_name">
      <a href="https://ad.danawa.com/banner?campaign=999">광고 배너</a>
    </p>
    <p class="price_sect"><strong>0</strong></p>
  </li>
</ul>
</body></html>
"""


# -- parse_search_html (순수 함수) --------------------------------------------


def test_parse_search_html_extracts_real_products_only():
    items = danawa.parse_search_html(_SAMPLE_HTML)

    assert len(items) == 2
    assert items[0]["product_name"] == "콜러노비타 리모컨 방수비데 BD-SD600"
    assert items[1]["product_name"] == "스노우피크 화로대SR 해외구매"


def test_parse_search_html_matches_prod_id_link_pattern():
    items = danawa.parse_search_html(_SAMPLE_HTML)

    assert items[0]["product_id"] == "1001"
    assert items[0]["price_krw"] == 184560


def test_parse_search_html_matches_pcode_link_pattern():
    items = danawa.parse_search_html(_SAMPLE_HTML)

    assert items[1]["product_id"] == "2002"
    assert items[1]["price_krw"] == 127700


def test_parse_search_html_skips_ad_slot_without_prod_link():
    """광고 슬롯(prod_id/pcode 없는 링크, 이름/가격 없는 빈 슬롯)은 제외된다."""
    items = danawa.parse_search_html(_SAMPLE_HTML)

    names = [it["product_name"] for it in items]
    assert "광고 배너" not in names


def test_parse_search_html_normalizes_protocol_relative_image_url():
    items = danawa.parse_search_html(_SAMPLE_HTML)

    assert items[0]["image_url"] == "https://gdimg.gmarket.co.kr/1/goodsimage.jpg"


def test_parse_search_html_returns_empty_list_for_no_results_page():
    items = danawa.parse_search_html("<html><body>검색 결과가 없습니다.</body></html>")

    assert items == []


# -- search_danawa 네트워크 래퍼 (httpx.MockTransport) ------------------------

_RealAsyncClient = httpx.AsyncClient


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def factory(**kwargs):
        return _RealAsyncClient(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"), headers=kwargs.get("headers"))

    monkeypatch.setattr(danawa.httpx, "AsyncClient", factory)


def test_search_danawa_returns_parsed_items_on_success(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SAMPLE_HTML)

    _patch_client(monkeypatch, handler)

    items = asyncio.run(danawa.search_danawa("콜러 비데", limit=10))

    assert len(items) == 2
    assert items[0]["product_name"] == "콜러노비타 리모컨 방수비데 BD-SD600"


def test_search_danawa_respects_limit(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SAMPLE_HTML)

    _patch_client(monkeypatch, handler)

    items = asyncio.run(danawa.search_danawa("콜러 비데", limit=1))

    assert len(items) == 1


def test_search_danawa_returns_empty_list_on_non_200_status(monkeypatch):
    """차단(403/418 등)되거나 서버 오류가 나도 예외를 던지지 않고 빈
    리스트를 돌려준다 - 보조 검증 수단이라 실패해도 파이프라인은 계속돼야
    한다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418)

    _patch_client(monkeypatch, handler)

    items = asyncio.run(danawa.search_danawa("아무거나"))

    assert items == []


def test_search_danawa_returns_empty_list_on_network_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    _patch_client(monkeypatch, handler)

    items = asyncio.run(danawa.search_danawa("아무거나"))

    assert items == []
