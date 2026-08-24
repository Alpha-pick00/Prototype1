"""다나와 어댑터(fetchers/danawa.py)를 파이프라인에 연결하는 계층.

STEP 3 설계(2026-08-10 지시서) 핵심:
- 다나와 페치는 LLM 호출과 asyncio.gather로 동시 실행한다(순차로 붙이면 그만큼
  느려지므로) — 이 모듈의 fetch_price_tables()가 그 진입점이다.
- 링크(구매 URL)를 못 만드는 offer도 가격표에서 버리지 않는다. 링크 생성
  가능 여부(linkable)와 판매처 신뢰도(trust)는 domain/url_rule을 아는지에
  달려 있고, 이 둘은 서로 다른 질문이다 - domain은 아는데 url_rule이 없는
  경우("몰은 확실한데 링크는 못 만든다")가 실제로 44개 offer 중 13개였다
  (검증 E).
- 44개 offer의 bridge_url을 파이프라인에서 해석하지 않는다. 최종 추천으로
  확정된 offer 1건에 대해서만 resolve_purchase_url()을 호출한다(lazy).
- 다나와 bridge URL이나 제휴 중계 URL은 이 모듈이 반환하는 어떤 값에도 담기지
  않는다 - PriceTableOffer 스키마 자체에 그 필드가 없고, 최종 추천에 쓰는
  resolve_purchase_url()도 완전히 해석된 최종 URL만 반환한다(실패 시 None).

정정(2026-08-12, robots.txt 재확인) - 위 "완전히 해석된 최종 URL"은 더 이상
사실이 아니다. prod.danawa.com/robots.txt가 `Disallow: /bridge/`를 명시하고
있다는 걸 뒤늦게 발견했다 - resolve_purchase_url()이 그동안 서버에서 직접
`/bridge/loadingBridge.html`을 페치해(2홉: 브릿지 -> 제휴 리다이렉트 URL
follow_redirects) 최종 판매처 URL을 만들어 왔는데, 이게 정확히 그 금지된
경로였다. 지금은 서버가 `/bridge/`를 아예 페치하지 않고, bridge_url 문자열
자체를 최종 "구매 링크"로 그대로 돌려준다 - resolve_purchase_url()이라는
이름은 남았지만 이제 네트워크를 전혀 안 쓴다(순수 문자열 가공). bridge_url은
다나와가 실사용자 브라우저에도 그대로 노출하는 자신의 1차 리다이렉트라
(robots.txt는 자동화된 크롤링을 막는 것이지 사람이 클릭하는 걸 막는 게
아니다) 이걸 돌려주는 건 문제없다 - 여전히 노출 안 하는 건 그 2홉 뒤에
나오는 제휴 중계 URL(다나와의 커미션 추적 URL)이다. 사용자는 클릭하면
다나와 자신의 리다이렉트 화면을 한 번 거쳐 최종 판매처로 이동한다(다나와
사이트에서 직접 사도 동일하게 거치는 화면).

STEP 6 설계(2026-08-11 지시서) 변경 - 매칭 키를 판매처+가격에서 상품명으로:
- STEP 5 라이브 검증에서 실패 5건 중 3건이 상품명은 완전 일치(100.0)였는데도
  판매처가 "다나와 가격비교" 자신이라 매칭이 원천 차단됐다. 다나와 페이지
  하나는 pcode 기준으로 특정 상품 하나를 가리키므로, LLM이 고른 상품과
  이름이 같으면 그 페이지의 10개 offer는 "그 상품의 가격표 그 자체"다 -
  판매처가 뭐든 상관없다.
- enrich_decision()은 이제 product_name만 본다(rapidfuzz token_set_ratio +
  모델명/규격·수량 토큰 충돌 가드). 가격 근접도·판매처 일치는 더 이상 보지
  않는다 - cheapest_linkable_raw_offer()가 A등급 중 최저가를 그냥 고른다.
- exclude_danawa_as_final_pick()은 별개 문제를 다룬다: 다나와 자신이 "판매처"로
  최종 추천되는 경우(라이브 검증에서 실제로 3/5 관찰됨) - 수수료 0%인 곳을
  추천하는 셈이라 pcode 일치로 확인된 자기 자신의 A등급 최저가로, 그것도
  없으면 다나와가 아닌 다른 에이전트 제안으로 바꾼다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator
from urllib.parse import parse_qsl, urlsplit

from rapidfuzz import fuzz

from fetchers import danawa, danawa_search
from fetchers.danawa_mall_map import CMPNYC_MAP, TRUST_TIER
from fusion.dedup import NAME_SIMILARITY_THRESHOLD

from .exclusive_tokens import exclusive_tokens_conflict
from .spec_match import model_or_quantity_conflict
from .schemas import (
    BrandOption,
    Decision,
    PriceTable,
    PriceTableOffer,
    Proposal,
    SearchResult,
)

logger = logging.getLogger(__name__)

DANAWA_HOST = "prod.danawa.com"
DANAWA_ROOT_DOMAIN = "danawa.com"
# 3 -> 5 (사용자 요청, 실제 사례: "옥수수수염차" 검색 시 원하던 상품이 다나와
# 자체 검색 관련도 순위 10위라 3개 상한으로는 못 잡았다). 5도 10위까지는
# 못 잡는다 - 다나와 관련도 순위와 가격 순위가 다르다는 게 근본 원인이라
# 어떤 고정 상한도 모든 케이스를 보장하지 않는다. 대신 요청량이 그만큼
# (검색 1건 + 상세 최대 5건 = 쿼리당 최대 6건) 늘어난다.
MAX_DANAWA_URLS = 5

# run_danawa_only_debate() 전용(사용자 요청, 2026-08-11: "한번에 5개 찾아주는거
# 너무 느린데"). LLM 경로(fetch_price_tables, MAX_DANAWA_URLS=5)는 후보를
# 넓게 모아 judge에게 선택지를 주는 게 목적이라 recall이 우선이지만, 이
# 경로는 LLM 없이 응답 시간 자체가 체감 품질이라 속도를 우선한다.
DANAWA_ONLY_SEARCH_LIMIT = 3

# check_clarify_facets() 전용(사용자 요청, 2026-08-12: "브랜드가 2,3개 정도만
# 뜨는데"). DANAWA_ONLY_SEARCH_LIMIT(3)를 그대로 쓰면 상품명 표본이 3개뿐이라
# 애초에 브랜드가 3개를 넘게 나올 수 없었다 - 이 경로는 상세페이지를 추가로
# 페치하지 않고(가격표 실측이 아니라 facet 추출용 상품명만 필요) search.danawa.com
# 검색 결과 페이지 하나를 그대로 더 많이 파싱하는 것뿐이라(fetchers/danawa_search.py
# search_danawa - limit은 이미 받아온 HTML에서 몇 개까지 파싱할지일 뿐, 네트워크
# 요청을 추가로 만들지 않는다) 크게 늘려도 10초 Crawl-delay 외의 비용이 없다.
#
# 30 -> 60(2026-08-13: "APLLE 을 선택했을때 시리즈 후보가 너무 적어") -> 90
# (2026-08-14: "보여주는 기종 수가 너무 적은데" - "핸드폰 케이스" 검색에서
# 다나와 실제 174개 갤럭시 호환 매물에 비해 표본이 너무 좁아 "핸드폰 기종"
# facet 다양성이 부족했다). 브랜드/기종별 다양성은 표본 수에 직접 달려있다 -
# "핸드폰"처럼 다나와 검색 결과가 특정 제조사 쪽으로 치우친 카테고리는 표본이
# 좁을수록 비주류 브랜드/기종 상품이 1~2개만 걸릴 수 있다. search_danawa()가
# 이제 항상 페이지당 최대 90개까지 파싱해 캐시해두므로(fetchers/danawa_search.py
# ::SEARCH_PAGE_PARSE_LIMIT), 90(=파싱 상한 그대로)으로 늘려도 추가 네트워크
# 요청은 없다 - 이미 캐시된 걸 전부 슬라이스해서 쓰는 것뿐이다.
CLARIFY_SEARCH_LIMIT = 90


def _query_param(url: str | None, name: str) -> str | None:
    if not url:
        return None
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True)).get(name)


def _domain_and_rule(offer: danawa.DanawaOffer) -> tuple[str | None, str | None]:
    """offer의 bridge_url에서 cmpnyc를 뽑아 CMPNYC_MAP과 대조한다. 네트워크
    없음 - 이미 페치해 둔 bridge_url 문자열만 파싱한다."""
    cmpnyc = _query_param(offer.get("bridge_url"), "cmpnyc")
    mapping = CMPNYC_MAP.get(cmpnyc) if cmpnyc else None
    if mapping is None:
        return None, None
    return mapping["domain"], mapping["url_rule"]


def _trust_for_domain(domain: str | None) -> float | None:
    """domain을 모르면 등급도 모른다 - None으로 남긴다(0.3으로 강등 금지,
    검증 E 지시서). domain은 알지만 TRUST_TIER 어디에도 없으면 0.3."""
    if domain is None:
        return None
    for tier, domains in TRUST_TIER.items():
        if domain in domains:
            return tier
    return 0.3


def select_danawa_urls(results: list[SearchResult], limit: int = MAX_DANAWA_URLS) -> list[str]:
    """Tavily 결과 중 다나와 상품 페이지만, score 내림차순으로 최대 limit개."""
    candidates = [r for r in results if urlsplit(r.url).netloc.lower() == DANAWA_HOST]
    candidates.sort(key=lambda r: r.score if r.score is not None else float("-inf"), reverse=True)
    return [r.url for r in candidates[:limit]]


def build_price_table(result: danawa.DanawaResult) -> PriceTable | None:
    """순수 함수 - 이미 페치된 DanawaResult를 등급 매긴 PriceTable로 바꾼다.
    네트워크 없음. parse_status가 ok/partial이고 offer가 하나 이상일 때만
    PriceTable을 만든다."""
    if result["parse_status"] not in ("ok", "partial") or not result["offers"]:
        return None

    sorted_offers = sorted(result["offers"], key=lambda o: o["price_krw"])
    graded: list[PriceTableOffer] = []
    for rank, offer in enumerate(sorted_offers, start=1):
        domain, url_rule = _domain_and_rule(offer)
        graded.append(
            PriceTableOffer(
                seller=offer["seller"],
                price_krw=offer["price_krw"],
                delivery_text=offer["delivery_text"],
                domain=domain,
                trust=_trust_for_domain(domain),
                linkable=url_rule is not None,
                rank=rank,
            )
        )

    prices = [o.price_krw for o in graded]
    spread = round(max(prices) / min(prices), 3) if min(prices) else None
    pcode = _query_param(result["source_url"], "pcode")
    is_partial = result["is_partial"]

    return PriceTable(
        source_pcode=pcode,
        product_name=result["product_name"],
        image_url=result.get("image_url"),
        offers=graded,
        spread=spread,
        total_mall_count=result["total_mall_count"],
        offers_shown=result["offers_shown"],
        is_partial=is_partial,
        price_label="확인된 최저가" if is_partial else "최저가",
    )


async def _search_danawa_items(query: str, limit: int = MAX_DANAWA_URLS) -> list[danawa_search.DanawaSearchItem]:
    """search.danawa.com 직접 검색(B-3) 결과 그대로. url만 필요하면
    _search_danawa_urls()를 쓰고, product_name까지 필요한 호출자(예: AI
    상세검색 facet 추출 - app.debate.check_clarify_facets)는 이 함수를 직접
    쓴다 - search.danawa.com의 Crawl-delay(10초)가 프로세스 전체에 걸려 있어서,
    같은 query에 대해 이 함수와 _search_danawa_urls()를 둘 다 부르면 검색을
    두 번 태워 불필요하게 그만큼 느려진다. 반드시 한 번만 호출해서 재사용할 것.

    막히거나(DanawaSearchBlocked) 실패해도 예외를 던지지 않고 빈 리스트를
    반환한다 - 호출자에 따라 폴백 경로가 다르므로(Tavily만으로 계속 진행,
    또는 "상세검색 불필요"로 취급 등) 예외로 전체를 막지 않는다."""
    try:
        return await danawa_search.search_danawa(query, limit=limit)
    except danawa_search.DanawaSearchBlocked:
        logger.warning("danawa direct search blocked for query=%r", query)
        return []
    except Exception:
        logger.exception("danawa direct search crashed for query=%r", query)
        return []


async def _search_danawa_categories(query: str) -> list[danawa_search.DanawaCategoryGroup]:
    """AI 상세검색 "카테고리" facet의 실측 출처(app.debate.check_clarify_facets) -
    _search_danawa_items와 같은 페이지/캐시를 공유하므로(danawa_search._fetch_entry)
    같은 query에 대해 이미 _search_danawa_items를 호출했다면 추가 네트워크
    요청이 없다. 호출부가 items가 비어있을 때(검색 실패/차단)는 아예 부르지
    않으므로, 여기서 다시 막히거나 실패해도 예외 없이 빈 리스트만 반환한다."""
    try:
        return await danawa_search.search_danawa_categories(query)
    except danawa_search.DanawaSearchBlocked:
        logger.warning("danawa category breakdown search blocked for query=%r", query)
        return []
    except Exception:
        logger.exception("danawa category breakdown search crashed for query=%r", query)
        return []


async def _search_danawa_urls(query: str, limit: int = MAX_DANAWA_URLS) -> list[str]:
    """search.danawa.com 직접 검색(B-3)으로 pcode를 찾아 상세페이지 URL로
    바꾼다. 성능 실측용 임시 배선 - Tavily 경로와 합집합으로만 쓰이므로,
    여기서 막히거나(DanawaSearchBlocked) 느려도 파이프라인은 Tavily만으로
    계속 동작해야 한다. search.danawa.com의 Crawl-delay(10초)가 프로세스
    전체에 걸려 있어서, 직전 10초 안에 이미 검색이 있었으면 이 호출이 그만큼
    그대로 느려진다 - 의도된 동작이다(로컬 성능 실측 목적)."""
    items = await _search_danawa_items(query, limit=limit)
    return [f"https://prod.danawa.com/info/?pcode={item['pcode']}" for item in items]


def _merge_and_cap_urls(tavily_urls: list[str], search_urls: list[str]) -> list[str]:
    """URL 출처는 Tavily(select_danawa_urls)와 다나와 직접 검색
    (_search_danawa_urls) 두 경로의 합집합이다 - 대체가 아니다. pcode
    기준으로 중복 제거 후 상한(MAX_DANAWA_URLS)을 그대로 적용한다."""
    urls: list[str] = []
    seen_keys: set[str] = set()
    for u in tavily_urls + search_urls:
        key = _query_param(u, "pcode") or u
        if key in seen_keys:
            continue
        seen_keys.add(key)
        urls.append(u)
    return urls[:MAX_DANAWA_URLS]


async def fetch_price_tables_for_urls(
    urls: list[str],
) -> list[tuple[PriceTable, danawa.DanawaResult]]:
    """이미 합집합+상한이 적용된 URL 목록을 그대로 페치한다. fetch_price_tables()의
    후반부를 분리한 것 - run_danawa_only_debate()가 Tavily 검색과 다나와 직접
    검색을 asyncio.gather로 병렬 실행하려면(둘이 서로 독립적이라 순차로 기다릴
    이유가 없다), URL을 미리 합쳐서 이 함수에 넘기는 형태가 필요했다.

    무슨 일이 있어도 예외를 던지지 않는다 - 실패하면 빈 리스트를 반환해
    본 파이프라인을 절대 막지 않는다. (PriceTable, DanawaResult) 튜플로
    반환하는 이유는 fetch_price_tables()와 동일(bridge_url 보존)."""
    if not urls:
        return []

    try:
        raw_results = await asyncio.gather(
            *(danawa.fetch_danawa_offers(u) for u in urls), return_exceptions=True
        )
    except Exception:
        logger.exception("danawa price table fetch crashed entirely")
        return []

    tables: list[tuple[PriceTable, danawa.DanawaResult]] = []
    for r in raw_results:
        if isinstance(r, BaseException):
            logger.info("danawa fetch failed for one url: %r", r)
            continue
        try:
            table = build_price_table(r)
        except Exception:
            logger.exception("failed to build price table from danawa result")
            continue
        if table is not None:
            tables.append((table, r))
    return tables


async def stream_price_tables_for_urls(
    urls: list[str],
) -> AsyncIterator[tuple[PriceTable, danawa.DanawaResult]]:
    """fetch_price_tables_for_urls()와 하는 일은 같지만(같은 URL 목록을
    페치해 PriceTable로 바꾼다), asyncio.gather처럼 전부 끝날 때까지
    기다리지 않고 asyncio.as_completed로 하나 끝나는 대로 즉시 내보낸다.

    사용자 요청(2026-08-11, "1개 서치 완료되면 1개 올려줘 먼저") - run_danawa_only_debate_stream()이
    이 제너레이터를 그대로 SSE 이벤트로 넘긴다. 실패한 URL은 조용히
    건너뛴다 - fetch_price_tables_for_urls()와 동일한 "본 파이프라인을
    절대 막지 않는다" 원칙."""
    if not urls:
        return

    tasks = [asyncio.ensure_future(danawa.fetch_danawa_offers(u)) for u in urls]
    for coro in asyncio.as_completed(tasks):
        try:
            raw = await coro
        except Exception:
            logger.info("danawa fetch failed for one url (stream)")
            continue
        try:
            table = build_price_table(raw)
        except Exception:
            logger.exception("failed to build price table from danawa result (stream)")
            continue
        if table is not None:
            yield table, raw


async def fetch_price_tables(
    query: str,
    results: list[SearchResult],
) -> list[tuple[PriceTable, danawa.DanawaResult]]:
    """LLM 호출들과 asyncio.gather로 나란히 실행되는 걸 전제로 한 진입점.
    무슨 일이 있어도 예외를 던지지 않는다 - 실패하면 빈 리스트를 반환해
    본 파이프라인(LLM 기반 추천)을 절대 막지 않는다.

    (PriceTable, DanawaResult) 튜플로 반환하는 이유: PriceTable은 응답에
    그대로 노출되는 공개 스키마라 bridge_url이 없다. 최종 추천 확정 후
    resolve_purchase_url()을 부르려면 원본 DanawaOffer(bridge_url 포함)가
    필요해서 함께 들고 다닌다 - bridge_url은 이 튜플 밖으로 나가지 않는다.

    results(Tavily)는 이미 호출자가 await해서 갖고 있다는 전제다
    (run_single_debate가 LLM 에이전트들과 결과를 공유하므로) - 그래서 여기서는
    다나와 직접 검색만 추가로 기다린다. Tavily 자체를 병렬화하고 싶으면
    run_danawa_only_debate()처럼 _search_danawa_urls()/select_danawa_urls()를
    호출자 쪽에서 직접 gather로 묶고 fetch_price_tables_for_urls()를 써라."""
    tavily_urls = select_danawa_urls(results)
    search_urls = await _search_danawa_urls(query)
    urls = _merge_and_cap_urls(tavily_urls, search_urls)
    return await fetch_price_tables_for_urls(urls)


def pick_primary(
    tables: list[tuple[PriceTable, danawa.DanawaResult]],
) -> tuple[PriceTable, danawa.DanawaResult] | None:
    """여러 다나와 URL이 페치됐을 때 offer가 가장 많은(=가장 풍부한) 페이지를
    대표 가격표로 쓴다."""
    if not tables:
        return None
    return max(tables, key=lambda pair: len(pair[0].offers))


# 같은 상품(스펙 변형)인지 완전히 다른 상품인지 가르는 이름 유사도 경계.
# 실측(2026-08-11, "맥북에어 m2" vs "노트북"): 같은 모델의 색상/용량 변형끼리는
# rapidfuzz.fuzz.token_set_ratio가 88.6~100, 브랜드/모델 자체가 다른
# 상품끼리는 32.9~46.5로 뚜렷하게 갈렸다 - 그 사이 65를 경계로 쓴다.
FAMILY_SIMILARITY_THRESHOLD = 65.0


def _is_single_product_family(tables: list[tuple[PriceTable, danawa.DanawaResult]]) -> bool:
    """search_danawa()로 얻은 후보 페이지들이 (스펙만 다른) 같은 상품의
    변형들인지 판별한다. tables[0]을 기준으로 삼는다 - fetch_price_tables_for_urls가
    입력 URL 순서(=다나와 검색 관련도 순위)를 그대로 보존하므로 tables[0]이
    다나와가 가장 관련도 높다고 판단한 상품이다.

    "노트북"처럼 브랜드/모델 자체가 갈리는 카테고리 검색어를 이걸로 걸러낸다 -
    걸러지면 하나를 억지로 고르지 않고 build_ambiguous_options()로 후보를
    나열한다(사용자 요청, 2026-08-11: "'노트북'말고도 다른 애매모호한것들을
    검색했을때... 옵션을 선택하게 해줬으면 좋겠어")."""
    if len(tables) <= 1:
        return True
    names = [t.product_name or "" for t, _ in tables]
    ref = names[0]
    return all(fuzz.token_set_ratio(ref, n) >= FAMILY_SIMILARITY_THRESHOLD for n in names[1:])


def cheapest_across_tables(
    tables: list[tuple[PriceTable, danawa.DanawaResult]],
) -> tuple[PriceTable, danawa.DanawaResult, danawa.DanawaOffer] | None:
    """같은 상품의 변형 후보들(_is_single_product_family가 True인 경우)을
    통틀어 A등급 중 절대 최저가 offer 하나를 고른다.

    pick_primary()(offer 개수가 가장 많은 테이블 = "가장 풍부한" 페이지)와는
    다른 기준이다 - 실측(맥북에어 M2, 2026-08-11)에서 검색 관련도 1위 변형의
    A등급 최저가가 1,307,990원인데도, offer가 하나 더 많다는 이유만으로
    1,669,990원짜리 변형이 최종 추천으로 나갔다. "다나와 인기상품가와
    비슷하거나 그보다 싼 걸 추천해달라"는 사용자 요청에는 풍부함이 아니라
    가격이 기준이어야 한다."""
    best: tuple[PriceTable, danawa.DanawaResult, danawa.DanawaOffer] | None = None
    for table, raw in tables:
        offer = cheapest_linkable_raw_offer(raw)
        if offer is None:
            continue
        if best is None or offer["price_krw"] < best[2]["price_krw"]:
            best = (table, raw, offer)
    return best


async def build_ambiguous_options(
    tables: list[tuple[PriceTable, danawa.DanawaResult]],
) -> list[BrandOption]:
    """_is_single_product_family()가 False일 때(검색어가 여러 서로 다른
    상품에 걸쳐 있을 때) 하나를 억지로 고르지 않고, 후보별 실측 최저가를
    가격 오름차순으로 나열한다. url은 이미 resolve_purchase_url까지 끝낸
    완전한 최종 구매 URL이라 프론트는 재요청 없이 바로 클릭-이동만 하면
    된다 - BulkDecideResponse와 동일한 BrandOption 스키마를 그대로 쓰므로
    프론트엔드의 기존 'bulk' 모드 렌더링을 그대로 재사용할 수 있다.

    후보별 resolve_purchase_url()을 asyncio.gather로 동시에 실행한다(사용자
    요청, 2026-08-11: "검색시간을 더 줄일수있나") - 원래 for 루프 안에서
    하나씩 await했는데, 이건 후보마다 독립적인 2홉 네트워크 요청을 굳이
    직렬로 줄 세우는 것이었다(3개면 최대 3배 느려짐). 후보끼리는 서로 다른
    판매처/도메인일 때가 많아 병렬화해도 안전하다."""
    candidates = [
        (table, raw, offer)
        for table, raw in tables
        if table.product_name and (offer := cheapest_linkable_raw_offer(raw)) is not None
    ]
    resolved_urls = await asyncio.gather(*(resolve_purchase_url(offer) for _, _, offer in candidates))

    scored: list[tuple[int, BrandOption]] = []
    for (table, _raw, offer), resolved_url in zip(candidates, resolved_urls):
        if resolved_url is None:
            continue
        scored.append(
            (
                offer["price_krw"],
                BrandOption(
                    brand=table.product_name,
                    product_name=offer["seller"],
                    price=f"{offer['price_krw']:,}원",
                    retailer=offer["seller"],
                    url=resolved_url,
                    reasoning="다나와 실측 최저가(A등급, 구매링크 검증됨)",
                ),
            )
        )
    scored.sort(key=lambda pair: pair[0])
    return [option for _, option in scored]


def cheapest_linkable_raw_offer(result: danawa.DanawaResult) -> danawa.DanawaOffer | None:
    """A등급(linkable) offer 중 최저가 원본(bridge_url 포함)을 찾는다.
    없으면 None - 이 경우 "링크 있는 추천"을 만들 수 없다는 뜻이다."""
    linkable = [
        offer for offer in result["offers"] if _domain_and_rule(offer)[1] is not None
    ]
    if not linkable:
        return None
    return min(linkable, key=lambda o: o["price_krw"])


async def resolve_purchase_url(offer: danawa.DanawaOffer) -> str | None:
    """최종 추천으로 확정된 offer 1건에 대해서만 호출한다(lazy) - 파이프라인
    다른 어디에서도 자동 호출되지 않는다. 이름과 달리(예전엔 실제로 네트워크로
    "해석"했다) 지금은 순수 문자열 가공뿐이라 네트워크 요청이 전혀 없다 -
    (2026-08-12) prod.danawa.com/robots.txt가 `Disallow: /bridge/`를 명시하고
    있다는 걸 뒤늦게 발견해서, 서버가 /bridge/를 직접 페치하던 방식(구
    danawa.resolve_outlink())을 폐기했다. async def는 호출부(await
    resolve_purchase_url(...))를 안 건드리려고 그대로 남겨뒀다.

    url_rule이 "template:..."이면 bridge_url의 link_pcode를 그대로 대입해
    조립한다(11번가 - 검증 E-1/D에서 확인: goUrl 파라미터의 목적지 상품 ID가
    다나와 자신의 link_pcode와 일치했다).
    "bridge_passthrough"면 bridge_url 문자열을 그대로 돌려준다(쿠팡/옥션/
    G마켓/SSG/롯데ON/SK스토아/신세계라이브쇼핑/신세계몰/이마트몰/현대Hmall) -
    bridge_url은 다나와가 실사용자 브라우저에도 그대로 노출하는 자신의 1차
    리다이렉트라(다나와에서 직접 사도 똑같이 거치는 화면) 그대로 돌려줘도
    문제없다. robots.txt Disallow는 자동화된 크롤링을 막는 것이지 사람이
    브라우저로 클릭하는 걸 막는 게 아니다. 여전히 노출 안 하는 건 그 뒤의
    제휴 중계 URL(다나와 커미션 추적) - 서버가 이제 그걸 아예 알지도
    못한다(2홉째를 더 이상 안 따라가므로)."""
    domain, url_rule = _domain_and_rule(offer)
    if url_rule is None:
        return None

    if url_rule.startswith("template:"):
        template = url_rule[len("template:") :]
        link_pcode = _query_param(offer.get("bridge_url"), "link_pcode")
        if not link_pcode:
            return None
        return template.format(link_pcode=link_pcode)

    if url_rule == "bridge_passthrough":
        return offer.get("bridge_url")

    logger.warning("unknown url_rule %r for domain %r - not resolving", url_rule, domain)
    return None


def _product_name_matches(decision_name: str, danawa_name: str) -> bool:
    if fuzz.token_set_ratio(decision_name, danawa_name) < NAME_SIMILARITY_THRESHOLD:
        return False
    if model_or_quantity_conflict(decision_name, danawa_name):
        return False
    # 백미/발아현미처럼 순한글 단어 하나가 결정적 차이인 경우 - token_set_ratio는
    # 공통 토큰이 많으면 이런 차이를 그냥 덮어버린다(실측 93.0점, 85 통과).
    if exclusive_tokens_conflict(decision_name, danawa_name):
        return False
    return True


# 수수료 0%인 가격비교 사이트(다나와) 자체를 최종 추천 판매처로 노출하면
# 안 된다는 원칙에 걸리는 도메인 집합. 에누리는 검색 비교 대상에서 완전히
# 빠졌으므로(2026-08-15) 더 이상 여기 포함하지 않는다.
PRICE_COMPARISON_DOMAINS = {DANAWA_ROOT_DOMAIN}


def _root_domain_matches(url: str | None, root: str) -> bool:
    if not url:
        return False
    host = urlsplit(url).netloc.lower()
    return host == root or host.endswith("." + root)


def _is_danawa_bridge_passthrough(url: str | None) -> bool:
    """(2026-08-12) resolve_purchase_url()이 bridge_passthrough 판매처의 최종
    "구매 링크"로 bridge_url(prod.danawa.com/bridge/loadingBridge.html?...)을
    그대로 돌려주게 되면서, 이 URL의 호스트가 danawa.com 서브도메인이라
    _is_price_comparison_domain()에 그대로 걸리는 버그가 있었다 - 다나와
    "자신"이 판매처로 뽑힌 게 아니라(retailer는 여전히 쿠팡/옥션 등 실제
    판매처다) 그리로 가는 다나와의 공식 리다이렉트일 뿐인데,
    exclude_price_comparison_site_as_final_pick()이 이를 오인해 멀쩡한
    danawa_offer 추천을 LLM 추측(때로는 완전히 다른 상품)으로 갈아치웠다.
    /bridge/ 경로는 그 자체로 "다나와가 최종 판매처"인 경우가 없으므로
    이 함수로 미리 걸러낸다.

    (2026-08-16 강화) 경로만 보고 "이미 검증된 링크"로 믿으면 안 된다 -
    relaxed fallback(gpt.pick_most_relevant)은 cheapest_linkable_raw_offer/
    resolve_purchase_url을 거치지 않고, LLM이 Tavily 원문 스니펫에 그대로
    박혀 있는 bridge_url 문자열을 아무 검증 없이 베껴올 수 있다 - 그 cmpnyc가
    CMPNYC_MAP에서 지금 실제로 url_rule="bridge_passthrough"인 판매처인지까지
    확인해야, cmpnyc가 깨진 것으로 확인돼 None으로 내려간 판매처(예: 쿠팡
    TP40F)의 죽은 링크를 "이미 정상"이라고 오인해 통과시키지 않는다."""
    if not url:
        return False
    if not (_root_domain_matches(url, DANAWA_ROOT_DOMAIN) and urlsplit(url).path.startswith("/bridge/")):
        return False
    cmpnyc = _query_param(url, "cmpnyc")
    mapping = CMPNYC_MAP.get(cmpnyc) if cmpnyc else None
    return mapping is not None and mapping["url_rule"] == "bridge_passthrough"


def _is_danawa_domain(url: str | None) -> bool:
    return _root_domain_matches(url, DANAWA_ROOT_DOMAIN) and not _is_danawa_bridge_passthrough(url)


def _is_price_comparison_domain(url: str | None) -> bool:
    if _is_danawa_bridge_passthrough(url):
        return False
    return any(_root_domain_matches(url, root) for root in PRICE_COMPARISON_DOMAINS)


async def enrich_decision(decision: Decision, raw_result: danawa.DanawaResult) -> Decision:
    """LLM judge가 고른 decision을 다나와 실측 가격표와 대조한다. 매칭 키는
    상품명이다(판매처+가격 아님 - STEP 5 라이브 검증에서 판매처 기준이 진짜
    같은 상품 3건을 전부 놓쳤다). 다나와 페이지 하나는 pcode로 특정 상품
    하나를 가리키므로, 이름이 같으면 그 페이지의 offer 전부가 그 상품의
    가격표다 - 판매처가 뭐든 상관없이 A등급 중 최저가를 쓴다.

    이름이 안 맞거나(rapidfuzz + 모델/수량 토큰 가드), A등급 offer가 없거나,
    링크 해석이 실패하면 손대지 않는다(price_source="llm_guess" 유지) -
    다른 상품으로 억지로 바꿔치기하지 않는다."""
    danawa_name = raw_result.get("product_name")
    if not danawa_name or not decision.product_name:
        return decision
    if not _product_name_matches(decision.product_name, danawa_name):
        return decision

    offer = cheapest_linkable_raw_offer(raw_result)
    if offer is None:
        return decision  # A등급이 없다 - 링크 없는 추천은 내지 않는다.

    resolved_url = await resolve_purchase_url(offer)
    if resolved_url is None:
        return decision

    decision.price = f"{offer['price_krw']:,}원"
    decision.retailer = offer["seller"]
    decision.url = resolved_url
    decision.image_url = raw_result.get("image_url") or decision.image_url
    decision.price_source = "danawa_offer"
    return decision


def _find_table_by_pcode(
    tables: list[tuple[PriceTable, danawa.DanawaResult]], pcode: str
) -> tuple[PriceTable, danawa.DanawaResult] | None:
    for table, raw in tables:
        if _query_param(raw["source_url"], "pcode") == pcode:
            return table, raw
    return None


async def resolve_danawa_comparison_url(
    url: str, tables: list[tuple[PriceTable, danawa.DanawaResult]]
) -> tuple[str, int, str, str | None] | None:
    """다나와 가격비교 페이지 URL(prod.danawa.com/info?pcode=...)을 pcode
    일치로 이미 페치된 A등급(링크 검증됨) 최저가 오퍼로 해석한다
    (resolved_url, price_krw, retailer, image_url) - pcode 불일치·A등급 오퍼
    없음 등으로 해석 실패하면 None(호출부는 이 경우 원래 URL을 그대로 쓰거나
    후보 자체를 버려야 한다 - 안 검증된 값을 지어내지 않기 위함).

    원래 exclude_price_comparison_site_as_final_pick 안에만 있던 로직을 뽑아냈다
    (2026-08-18) - propose 단계 병합(_merge_proposals)에서도 같은 해석이
    필요해졌다: Qwen/Groq/DeepSeek이 다나와 검색 결과에서 고를 수 있는 URL은
    거의 전부 이 가격비교 페이지 형태뿐인데, 해석 없이 필터링(agents/base.py
    ::is_danawa_comparison_page)만 하면 세 제안자가 후보를 사실상 못 만들어
    후보 풀이 자주 0건이 됐다."""
    pcode = _query_param(url, "pcode")
    if pcode is None:
        return None
    match = _find_table_by_pcode(tables, pcode)
    if match is None:
        return None
    _, raw_result = match
    offer = cheapest_linkable_raw_offer(raw_result)
    if offer is None:
        return None
    resolved_url = await resolve_purchase_url(offer)
    if resolved_url is None:
        return None
    return resolved_url, offer["price_krw"], offer["seller"], raw_result.get("image_url")


async def exclude_price_comparison_site_as_final_pick(
    decision: Decision,
    proposals: list[Proposal],
    tables: list[tuple[PriceTable, danawa.DanawaResult]],
) -> Decision:
    """다나와는 수수료 0%인 가격비교 사이트라 최종 추천 판매처가 될 수 없다
    (이 어댑터를 만든 이유 자체가 그거다) - 그런데 라이브 검증에서 judge가
    실제로 다나와 자신을 "판매처"로 고르는 사례가 3/5 관찰됐다.
    decision.url이 다나와면:
    1) pcode가 일치하는(=같은 상품이 확실한) 페치 결과가 있으면 그 A등급
       최저가로 교체한다(상품명 대조 불필요 - pcode 일치가 더 강한 증거).
    2) 없으면 가격비교 사이트가 아닌 다른 에이전트 제안으로 넘어간다
       (price_source는 여전히 llm_guess - 검증된 게 아니라 그냥 다른 LLM
       추측이다).
    3) 그것도 없으면 URL을 그대로 두고 경고 로그만 남긴다 - 노출은 불가피하다."""
    if not _is_price_comparison_domain(decision.url):
        return decision

    if _is_danawa_domain(decision.url):
        resolved = await resolve_danawa_comparison_url(decision.url, tables)
        if resolved is not None:
            resolved_url, price_krw, retailer, image_url = resolved
            decision.price = f"{price_krw:,}원"
            decision.retailer = retailer
            decision.url = resolved_url
            decision.image_url = image_url or decision.image_url
            decision.price_source = "danawa_offer"
            return decision

    for proposal in proposals:
        if proposal.error is not None or not proposal.url or not proposal.product_name:
            continue
        if _is_price_comparison_domain(proposal.url):
            continue
        decision.product_name = proposal.product_name
        decision.price = proposal.price or decision.price
        decision.retailer = proposal.retailer or decision.retailer
        decision.url = proposal.url
        decision.image_url = proposal.image_url or decision.image_url
        decision.reasoning = proposal.reasoning or decision.reasoning
        return decision

    logger.warning(
        "final decision still points at a price-comparison site and no fallback exists "
        "(url=%s) - exposing it to the user is unavoidable here", decision.url
    )
    return decision
