"""ADK(Google Agent Development Kit) 기반 8단계 SequentialAgent 파이프라인.

2026-08-20에 제거된 옛 다나와/멀티에이전트 ADK 파이프라인(refine -> search ->
propose(ParallelAgent) -> filter_merge -> extract_pages -> challenge ->
apply_challenge -> judge)의 "구조"를 지금의 11번가 단일 소스 파이프라인
위에 재구성한다(2026-08-25 사용자 요청 - "8단계 SequentialAgent 흐름을 지금
파이프라인에 적용"). 데이터 소스(11번가 하나)와 모델 배정(Qwen/DeepSeek/HCX)은
`debate.py`가 이미 검증해둔 걸 그대로 재사용하고, 프레임워크만 ADK로
되돌린다 - 다나와 스크래핑이나 propose 6-way 병렬 구조를 되살리는 게
아니다.

refine(Qwen) -> search(11번가) -> propose(ParallelAgent, 11번가 단일 소스라
사실상 1-way) -> filter_merge(관련성 필터 + HCX 표기변형 폴백 + 임베딩 랭킹)
-> extract_pages(product_code 기준 중복 제거 - 11번가는 별도 상세조회 API가
없어 재조회할 게 없다) -> challenge(DeepSeek 의미 재검증) ->
apply_challenge(검증 결과를 Proposal에 반영) -> judge(Qwen 최종 선택) 순서.

SequentialAgent/ParallelAgent는 google-adk 2.6.3 기준 deprecated 표시가
있지만(대체 예정인 Workflow가 아직 미성숙) 실제로 정상 동작한다 - 옛
파이프라인 때 스파이크로 검증됐고, 이번에 임포트 재확인도 마쳤다.

challenge/judge는 옛 파이프라인과 달리 모델 호출이 실패해도 파이프라인
전체를 죽이지 않는다(on_model_error_callback으로 "검증 없음"/"판단 없음"
빈 응답을 합성해 계속 진행) - 옛 파이프라인은 이 경우를 하드 실패시키고
relaxed_fallback/comparison_page_listing_fallback 같은 다나와 전용 완화
폴백으로 이어받았는데, 이번 재구성에서는 그 폴백들 자체를 스코프에서 뺐다
(11번가는 구조화 데이터라 "일단 보여주기"식 완화가 필요 없음). 대신 지금
`debate.py`의 `_build_decision`이 이미 갖고 있는 "judge 실패 시 최저가
규칙 폴백"을 그대로 재사용한다 - 그래야 challenge/judge 중 하나가 실패해도
검색 자체는 성공했는데 결과를 아예 못 주는 회귀가 안 생긴다.

refine/challenge/judge(실제 LLM을 부르는 3단계) 모두 `app.llm_cache`
(Supabase KV+시맨틱, HITL의 facet 추출이 이미 쓰던 것과 같은 모듈)로
캐시된다(2026-08-26) - before_model_callback에서 캐시를 먼저 찾아보고
히트하면 실제 모델 호출 자체를 건너뛴다. challenge/judge는 질의 텍스트만이
아니라 후보 목록 서명(_candidate_cache_signature)까지 캐시 키에 넣는다 -
같은 질의라도 재고/가격 변동으로 후보 구성이 달라지면 옛 index 기준
verdict/판단을 새 후보에 잘못 재사용하게 되기 때문이다. on_model_error_
callback의 폴백 응답(빈 verdicts/index=-1)은 캐시하지 않는다 - API 실패를
캐시해버리면 다음 동일 요청도 영원히 실패한 것처럼 처리된다.

judge는 관련성 필터를 통과한 후보가 정확히 1개면 LLM 호출 자체를 건너뛴다
(2026-08-27, LLM 제거 가능 지점 분석 - `_skip_judge_if_single_candidate`).
"여러 후보 중 고른다"는 judge의 존재 이유가 후보 1개일 땐 성립하지 않는다 -
단, 그 후보가 challenge에서 verified=False로 나왔으면 스킵하지 않는다
(judge가 왜 검증 실패한 후보를 골랐는지 설명하는 reasoning은 여전히 LLM
판단 영역).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncGenerator, AsyncIterator

from google.adk.agents import BaseAgent, LlmAgent, ParallelAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events import Event, EventActions
from google.adk.models import LlmResponse
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel

from fetchers import elevenst

from . import llm_cache
from . import price_table as price_table_module
from .agents import gpt
from .agents.base import build_recommend_prompt, build_refine_query_prompt, parse_json_object
from .config import settings
from .debate import (
    _build_decision,
    _build_price_miss_decision,
    _build_proposals,
    _format_price_condition,
    _price_distance,
    _price_in_range,
    _refine_base_query,
    _search_candidates,
)
from .intent import extract_price_range, looks_conversational_query
from .schemas import DecideResponse, RefinedQuery

logger = logging.getLogger(__name__)

_APP_NAME = "alpha_pick_adk_pipeline"

# challenge(DeepSeek 의미 재검증)는 이번 재구성에서 새로 생긴 추가 LLM
# 호출이라 비용을 상위 몇 개로 제한한다(gpt.candidate_notes의
# _MAX_CANDIDATE_NOTES=5, 옛 파이프라인의 _MAX_EXTRACT_CANDIDATES=10과 같은
# 원칙 - "상위 후보만 추가로 신경 쓴다").
_MAX_CHALLENGE_CANDIDATES = 10

# _FilterMergeNode의 관련성 필터 통과율 하한(2026-08-27, 사용자 확인) -
# 실측으로 구체적 모델명 검색어(아이폰 17=50%, 갤럭시S25=77%)와 카테고리성
# 검색어(노트북 추천=3%, 컴퓨터=0%) 사이 뚜렷한 격차를 확인했다. 다만
# 정확한 모델명이어도 표기 차이(나이키 에어포스1=13%, "에어포스1"/"에어
# 포스 1" 띄어쓰기 차이로 낮게 나옴 - 걸러진 것도 전부 진짜 관련상품)로
# 낮게 나올 수 있어, 10%는 "카테고리성 검색어만 걸러내고 정밀도가 떨어지는
# 정상 케이스는 건드리지 않는" 절충값이다.
_CATEGORY_QUERY_MIN_MATCH_RATIO = 0.1

# 2026-08-26 - 8단계 중 실제 LLM을 부르는 refine/challenge/judge에
# app.llm_cache(기존 AI 상세검색 facet 추출이 쓰던 것과 같은 Supabase KV+
# 시맨틱 캐시)를 붙인다 - 회귀 테스트처럼 같은 질의가 반복되면 토큰/지연
# 비용 없이 이전 응답을 재사용한다. namespace는 HITL의 "clarify_facets"와
# 겹치지 않게 단계별로 분리한다.
_REFINE_CACHE_NAMESPACE = "pipeline_refine"
_CHALLENGE_CACHE_NAMESPACE = "pipeline_challenge"
_JUDGE_CACHE_NAMESPACE = "pipeline_judge"


def _candidate_cache_signature(items: list[dict]) -> str:
    """challenge/judge 캐시 키에 후보 구성을 반영한다 - 같은 질의라도 재고/
    가격 변동으로 검색 결과가 달라지면 다른 키로 취급해야, 옛 후보 순서에
    매겨진 index(verdicts/judge_result)를 새 후보에 잘못 재사용하지 않는다.
    refine은 후보와 무관(질의 정제는 검색 전 단계)이라 이 서명이 필요 없다."""
    return "|".join(f"{it.get('product_code') or it.get('url')}:{it['price_krw']}" for it in items)


def _pipeline_cache_key(query: str, candidates: list[dict], extra: str = "") -> str:
    return f"{query}{extra}::{_candidate_cache_signature(candidates)}"


def _llm_response_text(llm_response: LlmResponse) -> str:
    """ADK의 `__maybe_save_output_to_state`와 같은 필터를 쓴다 - thinking을
    지원하는 모델(Qwen 등)은 reasoning_content를 별도의 thought=True Part로
    돌려주는데(LiteLlm이 변환), 이걸 안 걸러내면 최종 JSON 앞뒤로 영어
    추론 과정이 뒤섞여 parse_json_object가 여러 개의 `{...}` 조각을 통째로
    긁어버려 파싱이 실패한다(2026-08-26 실측 - judge 캐시가 이 필터 누락
    때문에 저장에 계속 실패했다)."""
    if not llm_response.content or not llm_response.content.parts:
        return ""
    return "".join(part.text for part in llm_response.content.parts if part.text and not part.thought)


class _ChallengeVerdict(BaseModel):
    index: int
    verified: bool
    note: str = ""


class _ChallengeResult(BaseModel):
    verdicts: list[_ChallengeVerdict] = []


class _JudgeVerdict(BaseModel):
    index: int
    reasoning: str = ""


_CHALLENGE_INSTRUCTIONS = (
    "당신은 쇼핑 후보 목록이 실제로 사용자 질의와 같은 상품을 가리키는지 다시 "
    "한번 의미적으로 검토하는 검증 에이전트입니다. 아래 후보들은 이미 텍스트 "
    "유사도 기준 1차 필터링을 통과했지만, 표기만 비슷할 뿐 실제로는 다른 "
    "상품일 가능성이 있는지 최종 확인하세요(예: 본품이 아니라 액세서리/부속품이 "
    "섞였는지, 완전히 다른 브랜드나 모델인지). 의심스러운 근거가 명확할 때만 "
    "verified를 false로 표시하고, 판단이 애매하면 true로 두세요(과도하게 "
    "걸러내지 마세요). 각 후보의 index를 그대로 쓰고, note는 verified가 "
    "false일 때만 그 이유를 한 문장으로 쓰세요(true면 빈 문자열). 반드시 "
    "아래 JSON 형식으로만 답하세요. 다른 텍스트나 코드펜스를 덧붙이지 마세요.\n\n"
    '{"verdicts": [{"index": 0, "verified": true, "note": ""}]}'
)


def _build_challenge_prompt(query: str, candidates: list[dict]) -> str:
    lines = [
        f"[{i}] {c['product_name']} / {c['price_krw']:,}원 / 판매자: {c.get('seller')}"
        for i, c in enumerate(candidates)
    ]
    block = "\n".join(lines) or "(후보 없음)"
    return f"{_CHALLENGE_INSTRUCTIONS}\n\n사용자 질의: {query}\n\n후보:\n{block}"


def _parse_refine_result(text: str) -> dict:
    """refine(HCX) 응답을 {"query": "..."} dict로 파싱한다. output_schema
    없이(HCX가 json_schema/json_object 둘 다 거부해 2026-08-26에 뺐다)
    도는 이후로는 응답 형식이 프롬프트 지시를 안정적으로 안 지킨다(실측 -
    `{"query": "저렴한 아기 간식"}`을 지시했는데 그냥 `"저렴한 아기 간식"`
    (JSON 객체가 아니라 순수 문자열 리터럴)만 돌아온 사례 확인). 정상
    형식(`{...}`)을 먼저 시도하고, 실패하면 문자열 리터럴(`"..."`)로도
    시도한다 - 둘 다 안 되면 빈 dict(호출부가 원본 질의로 폴백)."""
    try:
        return parse_json_object(text)
    except (ValueError, TypeError):
        pass
    try:
        value = json.loads(text.strip())
    except (ValueError, TypeError):
        return {}
    return {"query": value} if isinstance(value, str) else {}


def _resolved_query(state: dict) -> str:
    """refine 단계 결과(refine_result.query)가 있으면 그걸, 없으면(스킵/실패)
    원본 질의를 쓴다. refine이 output_schema 없이 도는 이후로 `refine_result`
    는 ADK가 파싱해주지 않은 원문 텍스트일 수 있다 - `_parse_refine_result`로
    여기서 직접 파싱한다(challenge의 `_parse_challenge_result`와 동일한
    이유/패턴)."""
    refine_result = state.get("refine_result")
    if isinstance(refine_result, str):
        refine_result = _parse_refine_result(refine_result)
    refine_result = refine_result or {}
    return refine_result.get("query") or state.get("original_query") or ""


def _ranked_items(state: dict) -> list[elevenst.ElevenstSearchItem]:
    return state.get("ranked_items") or []


def _candidates_with_verdicts(state: dict) -> list[dict]:
    """judge 프롬프트용 후보 목록 - `_ranked_items`(가격/판매자 등 원본
    필드)에 apply_challenge가 만든 `proposals`의 verified/challenge_note를
    index 기준으로 덧씌운다(2026-08-26, 사용자 리포트 - "골프공 검색했는데
    골프파우치가 최종 추천으로 뜸"). judge는 이전까지 challenge 검증 결과를
    아예 못 보고 골랐다 - 두 검증 단계가 서로 대화를 안 하고 있었다."""
    proposals = state.get("proposals") or []
    merged = []
    for i, item in enumerate(_ranked_items(state)):
        candidate = dict(item)
        if i < len(proposals):
            candidate["verified"] = proposals[i].get("verified")
            candidate["challenge_note"] = proposals[i].get("challenge_note")
        merged.append(candidate)
    return merged


def _model_error_fallback_response(text: str) -> LlmResponse:
    """모델 호출이 실패했을 때 성공한 것처럼 대신 흘려보낼 최소 응답 - ADK가 이
    텍스트를 실제 모델 응답과 동일하게 output_key/output_schema 경로로 흘려
    보낸다(옛 adk_pipeline.py와 동일한 패턴)."""
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


class _ElevenstSearchNode(BaseAgent):
    """2단계 search - raw 모드(2026-08-27, 사용자 요청 - "HCX로 정제해서
    11번가 상위로 뜨는거 그대로 가져오면 되잖아"). refine 결과(순수 검색어)로
    11번가 웹사이트가 실제로 쓰는 랭킹을 그대로 가져온다.

    웹 랭킹 API로 교체(2026-08-27, 사용자 리포트 - "11번가에 아이폰 17
    검색하면 핸드폰으로 제대로 매핑되는데 API 통해서 하면 왜 액세서리가
    뜨는거야" -> "로컬에서 웹사이트에서 먼저 추천도순 가져오고 API에서
    매핑해주면 되잖아"). 실측 확인 - 공식 오픈 API(search_elevenst,
    ProductSearch)의 sortCd="A"는 실제 11st.co.kr 웹사이트가 사람에게
    보여주는 순위와 다른 알고리즘이라, 인기·고가 상품(아이폰/맥북 등)에서
    액세서리가 상단을 차지하는 경우가 흔했다. `elevenst.
    search_elevenst_web_ranking`이 웹사이트가 브라우저에서 직접 호출하는
    비공식 내부 API를 그대로 호출해, 판단·보정 없이도 웹사이트와 동일한
    순서(본품이 상단)를 받는다 - 그래서 sortCd=H 보정 재검색 같은 별도
    장치가 필요 없다.

    AI 상세검색 드릴다운(base_query/facet_answers)은 판단이 아니라
    "사용자가 화면에서 명시적으로 고른 조건을 반영하는 것"이라 raw 모드
    취지와 안 어긋난다(2026-08-27, 사용자 요청 - "AI 상세검색은 남겨놔야지") -
    base_query가 있으면 `_search_candidates`(debate.py, 공식 오픈 API 기반)를
    그대로 재사용해 넓게 검색한 뒤 사용자가 답한 facet 값으로 로컬
    필터링한다 - facet 추출·필터링 로직이 공식 API의 필드(ReviewCount 등)에
    맞춰져 있어 이 경로까지 웹 랭킹 API로 바꾸지는 않는다.

    가격 표현 제거(extract_price_range)는 검색어 전처리와 조건 반영을
    함께 한다 - "200만원대 컴퓨터"처럼 keyword에 가격 표현이 섞여 있으면
    검색 자체가 실패한다(실측 확인, 2026-08-27). `_run_pipeline_once`가
    refine 전에 이미 원본 질의에서 한 번 떼어(state의 price_min/max로
    저장) refine이 짧고 단순한 텍스트만 다듬도록 했지만(비결정성 완화),
    refine 결과에 표현이 다시 섞여 나올 수도 있으니 여기서도 한 번 더
    떼어 병합한다 - 사용자가 명시한 가격대 조건을 지키는 것은 "무엇을
    고를지 판단"이 아니라 "요청 사항을 반영"하는 것이라 raw 모드 취지와
    어긋나지 않는다(AI 상세검색 드릴다운과 같은 성격, 2026-08-27 사용자
    확인)."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        original_query = state.get("original_query", "")
        refined_query = _resolved_query(state)
        # refine 재시도(2026-08-27, 사용자 리포트 - "노트북 추천해줘"/"아이폰
        # 17을 진짜로 구매해보고 싶어졌어"가 정제 안 된 채로 그대로 검색어가
        # 됨). refine 단계 모델(HCX-DASH-002)이 같은 입력을 직접 호출하면
        # 매번 안정적으로 정제하는데, ADK LlmAgent 경로(시스템 프롬프트
        # 지시문 + 별도 user 메시지로 같은 텍스트가 중복 전달되는 구조)
        # 에서만 가끔 원문을 그대로(또는 {"error": null} 같은 지시 밖의
        # 필드까지 섞어) 돌려주는 비결정성이 실측 확인됐다.
        #
        # looks_conversational_query도 함께 확인해야 한다(2026-08-28 수정,
        # 실측 발견 - "음료수 500ml 병 탄산음료"처럼 대화체가 아니라
        # _skip_refine_if_already_specific이 애초에 LLM 호출 자체를 스킵하고
        # 원본을 그대로 반환한 케이스까지 "정제가 전혀 안 됐다"로 오판해
        # 여기서 강제로 HCX를 다시 불렀다. 이 재호출에서 HCX가 이미 구체적인
        # 검색어를 자기 방식대로 재구성하며 "500ml 병" 사이 공백을 없애버려
        # ("음료수500ml병탄산음료") 11번가 검색이 0건으로 실패했다 - 재시도의
        # 원래 의도(정제가 필요했는데 실패한 경우 보정)와 무관한 부작용이었다.
        # looks_conversational_query가 참일 때만(=정제가 필요해서 이 단계까지
        # 왔는데 결과가 원본과 같은, 진짜 실패 신호일 때만) 재시도한다.
        if (
            refined_query
            and refined_query.strip() == original_query.strip()
            and looks_conversational_query(original_query)
        ):
            retried = await gpt.refine_query(refined_query)
            if retried:
                refined_query = retried
        query, price_min, price_max = extract_price_range(refined_query)
        price_min = price_min if price_min is not None else state.get("price_min")
        price_max = price_max if price_max is not None else state.get("price_max")
        base_query = await _refine_base_query(state.get("base_query"), original_query, query)
        facet_answers = state.get("facet_answers")

        if base_query and base_query.strip() and base_query.strip() != query.strip():
            items = await _search_candidates(query, base_query, facet_answers)
        else:
            items = await elevenst.search_elevenst_web_ranking(
                query, limit=price_table_module.SINGLE_QUERY_SEARCH_LIMIT
            )

        yield Event(
            author=self.name,
            actions=EventActions(
                state_delta={
                    "search_items": items,
                    "resolved_query": query,
                    "resolved_base_query": base_query,
                    "price_min": price_min,
                    "price_max": price_max,
                }
            ),
        )


class _ElevenstProposeNode(BaseAgent):
    """3단계 propose(ParallelAgent 소속, 11번가 단일 소스라 사실상 1-way) -
    search 결과를 그대로 candidate 풀로 포장한다. LLM 추정이 필요 없다 -
    11번가 응답 자체가 이미 1st-party 구조화 데이터라 "제안"할 게 아니라
    "그대로 쓸" 데이터다. ParallelAgent 형태 자체는 유지해, 나중에 다른
    구조화 소스가 늘어나면 이 자리에 형제 노드를 추가하기만 하면 된다."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        items = ctx.session.state.get("search_items") or []
        yield Event(author=self.name, actions=EventActions(state_delta={"candidates": items}))


class _FilterMergeNode(BaseAgent):
    """4단계 filter_merge - raw 모드(2026-08-27, 사용자 요청 - "HCX로 정제해서
    11번가 상위로 뜨는거 그대로 가져오면 되잖아, 판단 없이"). 등급 보정·
    의심 후보 후순위화·challenge/judge 같은 판단 로직은 전부 없애고, search
    단계가 가져온 검색 결과(웹 랭킹 API)를 사실상 그대로 통과시킨다.

    단, 아래 두 가지는 다시 둔다 - 둘 다 "여러 후보 중 뭐가 나은지 판단"이
    아니라 "질의/요청 조건에 안 맞는 걸 구조적으로 거르거나 반영"하는
    것이라 raw 모드 취지와 어긋나지 않는다:

    1. 관련성 필터(`_product_name_matches`, 2026-08-27, 사용자 리포트 -
       "11번가에 아이폰 17 검색하면 핸드폰으로 제대로 매핑되는데 왜 API
       통해서 하면 액세서리가 뜨는거야?") - "이 상품이 질의가 가리키는
       상품과 같은 부류인지"(모델/수량 충돌, 본품 vs 부속품 구분)만
       구조적으로 거른다. 이 함수는 원래 서로 다른 소스가 "같은 상품"인지
       중복 판정하는 용도(fusion.dedup.NAME_SIMILARITY_THRESHOLD=85)라,
       "노트북 추천"처럼 구체적 모델명이 없는 카테고리성 검색어에는 안
       맞는다(실측 - "MSI 모던 16S..." 같은 상세 모델명과 문자열 유사도가
       5점대로 나와 30개 중 29개가 걸러짐, 2026-08-27). 통과율이 낮으면
       (_CATEGORY_QUERY_MIN_MATCH_RATIO 미만) 필터 자체가 이 질의에 안
       맞는다고 보고 판단 개입 없이 원본을 그대로 쓴다 - 필터를 통과한
       후보가 0건일 때(카테고리 검색어의 극단적인 경우)도 이 규칙 안에
       포함된다.

    2. 가격 범위 필터(_ElevenstSearchNode가 떼어낸 price_min/max,
       2026-08-27, 사용자 리포트 - "200만원대 노트북"이 가격대와 무관한
       결과를 줌) - 범위 안에 드는 후보가 있으면 그걸로 좁히고, 하나도
       없으면 judge를 부르지 않고(_skip_judge_always와 별개로 여기서
       미리) 가격이 가장 가까운 순으로 정렬해 price_miss_note를 남긴다 -
       run_stream이 이를 보고 규칙 기반으로 안내한다."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        query = state.get("resolved_query", "")
        facet_answers = state.get("facet_answers")
        items = state.get("candidates") or []
        if not items:
            raise RuntimeError(f"11번가에서 '{query}'에 대한 검색 결과를 찾지 못했습니다.")
        # facet_answers가 있으면 search 단계(_search_candidates)가 이미 사용자가
        # 고른 조건으로 구조적 필터링을 해둔 상태다 - 여기서 원래 질의 문자열
        # 기준 유사도 필터를 또 걸면(드릴다운 질의는 짧은 값 하나만 남는 경우가
        # 많아) 방금 필터링한 후보까지 이중으로 걸러낼 수 있어 건너뛴다.
        if facet_answers:
            ranked = items
        else:
            relevant = [it for it in items if price_table_module._product_name_matches(query, it["product_name"])]
            match_ratio = len(relevant) / len(items)
            ranked = items if match_ratio < _CATEGORY_QUERY_MIN_MATCH_RATIO else relevant

        price_min = state.get("price_min")
        price_max = state.get("price_max")
        if price_min is not None or price_max is not None:
            in_range = [it for it in ranked if _price_in_range(it["price_krw"], price_min, price_max)]
            if in_range:
                ranked = in_range
            else:
                price_miss_note = _format_price_condition(price_min, price_max)
                ranked = sorted(ranked, key=lambda it: _price_distance(it["price_krw"], price_min, price_max))
                yield Event(
                    author=self.name,
                    actions=EventActions(
                        state_delta={
                            "ranked_items": ranked,
                            "excluded_grade_tokens": [],
                            "price_miss_note": price_miss_note,
                        }
                    ),
                )
                return

        yield Event(
            author=self.name,
            actions=EventActions(state_delta={"ranked_items": ranked, "excluded_grade_tokens": []}),
        )


def _dedupe_items(items: list[elevenst.ElevenstSearchItem]) -> list[elevenst.ElevenstSearchItem]:
    """5단계 extract_pages의 핵심 로직(순수 함수, ADK 컨텍스트 없이 테스트
    가능) - product_code 기준 중복 제거, 없으면 url, 그마저 없으면 상품명으로
    폴백. 먼저 나온(랭킹 순서상 더 관련도 높은) 항목을 남긴다."""
    seen: set[str] = set()
    deduped: list[elevenst.ElevenstSearchItem] = []
    for item in items:
        key = item.get("product_code") or item.get("url") or item.get("product_name", "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


class _DedupPagesNode(BaseAgent):
    """5단계 extract_pages - 옛 파이프라인은 이 단계에서 후보 판매 페이지를
    실시간 재조회했지만, 11번가 오픈 API는 별도 상세조회 엔드포인트가 없고
    ProductSearch 응답 자체가 이미 완전한 구조화 데이터라 재조회할 대상이
    없다. 대신 이 자리에서 product_code 기준 중복(같은 상품이 여러 페이지에
    걸쳐 중복 노출되는 경우)을 제거한다 - 순수 no-op은 아니고, 상세조회
    자리를 실제로 쓸 소스가 늘어나면 여기서 페이지 재조회를 하게 될
    지점이다."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        deduped = _dedupe_items(ctx.session.state.get("ranked_items") or [])
        yield Event(author=self.name, actions=EventActions(state_delta={"ranked_items": deduped}))


def _parse_challenge_result(raw: Any) -> dict:
    """challenge(DeepSeek) 응답을 dict로 파싱한다. challenge는 더 이상
    ADK output_schema를 쓰지 않는다 - DeepSeek API가 `response_format:
    json_schema`(output_schema가 LiteLlm을 거쳐 요청하는 strict 구조화
    출력 모드)를 "This response_format type is unavailable now"로 거부해
    매 호출이 실패했었다(2026-08-26 확인). 대신 이 코드베이스의 다른 모든
    게이트(gpt.py)와 같은 패턴 - 프롬프트로 JSON 형식만 지시하고
    `parse_json_object`로 직접 파싱 - 으로 바꿔, DeepSeek이 이미 지원하는
    `json_object` 모드로 호출한다. 파싱 실패(모델이 형식을 안 지켰거나
    on_model_error_callback의 폴백 텍스트가 아닌 경우)는 "검증 없음"과
    동일하게 빈 dict로 흘려보낸다 - challenge 실패가 파이프라인 전체를
    막으면 안 된다는 원칙은 그대로 유지."""
    if isinstance(raw, dict):
        return raw
    try:
        return parse_json_object(str(raw or ""))
    except (ValueError, TypeError):
        logger.warning("challenge 응답 JSON 파싱 실패 - 검증 없이 계속 진행: %r", raw)
        return {}


async def _apply_challenge_verdicts(
    ranked: list[elevenst.ElevenstSearchItem], challenge_result: dict, query: str = ""
) -> list[dict]:
    """7단계 apply_challenge - raw 모드(2026-08-27, 사용자 요청 - "판단 없이
    그대로 가져오면 되잖아"). challenge 자체가 스킵되므로(challenge_result는
    항상 빈 verdicts) 순서·선택에 영향을 주는 재판정은 하지 않고
    `_build_proposals`(기본값 verified=True)를 그대로 쓴다 - 상품명에
    "케이스"가 있어도 걸러내거나 순서를 바꾸지 않는다.

    각 후보별 추천 이유(2026-08-27, 사용자 요청 - "각 후보군별로 추천 이유도
    설명해줘 너가 reasoning 해서") - gpt.candidate_notes(HCX)가 상위 후보
    (_MAX_CANDIDATE_NOTES개)마다 1문장 이유를 붙인다. 이건 순서를 바꾸는
    "판단"이 아니라 이미 정해진 순서에 대한 "설명"이라 raw 모드 취지와
    안 어긋난다 - 실패해도(키 없음·API 오류) candidate_notes가 빈 dict를
    돌려줘 _build_proposals의 기본 문구로 안전하게 대체된다."""
    notes = await gpt.candidate_notes(query, ranked)
    return [p.model_dump() for p in _build_proposals(ranked, notes)]


class _ApplyChallengeNode(BaseAgent):
    """7단계 apply_challenge - 로직은 `_apply_challenge_verdicts`(순수 함수)에
    있고, 이 노드는 세션 상태를 읽고/쓰는 얇은 ADK 래퍼일 뿐이다."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        ranked = ctx.session.state.get("ranked_items") or []
        challenge_result = _parse_challenge_result(ctx.session.state.get("challenge_result"))
        query = _resolved_query(ctx.session.state)
        proposals = await _apply_challenge_verdicts(ranked, challenge_result, query)
        yield Event(author=self.name, actions=EventActions(state_delta={"proposals": proposals}))


def _skip_refine_if_already_specific(callback_context, llm_request) -> LlmResponse | None:
    """1단계 refine - 이미 구체적인 질의는 LLM 왕복 자체를 건너뛴다. 지금
    파이프라인의 `_maybe_refine_query`가 `looks_conversational_query`로 거는
    것과 같은 게이트를 ADK의 before_model_callback으로 재현한다(옛
    `_skip_refine_if_already_specific`과 동일한 목적)."""
    original_query = callback_context.state.get("original_query", "")
    if not original_query or not looks_conversational_query(original_query):
        fallback = RefinedQuery(query=original_query)
        return _model_error_fallback_response(fallback.model_dump_json())
    return None


def _on_refine_error(callback_context, llm_request, error) -> LlmResponse | None:
    """refine(Qwen) 호출이 실패해도 원본 질의로 계속 진행한다 - 정제는 있으면
    좋은 보조 기능이지, 검색 자체를 막을 이유가 아니다(지금
    `gpt.refine_query`가 실패 시 None을 돌려줘 호출부가 원문을 그대로 쓰는
    것과 같은 원칙)."""
    logger.warning("refine 단계 모델 호출 실패 - 원본 질의로 계속 진행", exc_info=error)
    original_query = callback_context.state.get("original_query", "")
    fallback = RefinedQuery(query=original_query)
    return _model_error_fallback_response(fallback.model_dump_json())


async def _refine_cache_lookup(callback_context, llm_request) -> LlmResponse | None:
    """`_skip_refine_if_already_specific` 다음에 붙는 두 번째 before_model_
    callback(리스트 순서상 그게 먼저 걸러내고 남은, 즉 "정제가 실제로
    필요한" 질의만 여기까지 온다) - 같은 원본 질의를 HITL의 facet 캐시와
    같은 방식(exact + semantic)으로 재사용한다."""
    original_query = callback_context.state.get("original_query", "")
    if not original_query:
        return None
    cached = await llm_cache.exact_get(
        _REFINE_CACHE_NAMESPACE, original_query
    ) or await llm_cache.semantic_get(_REFINE_CACHE_NAMESPACE, original_query)
    if cached is None:
        return None
    return _model_error_fallback_response(json.dumps(cached, ensure_ascii=False))


async def _refine_cache_store(callback_context, llm_response: LlmResponse) -> None:
    """실제 refine 호출이 성공했을 때만(캐시 히트/스킵은 이 콜백 자체를 안
    탄다 - before_model_callback이 응답을 대신하면 after_model_callback은
    호출되지 않는 ADK의 동작) 결과를 캐시에 저장한다."""
    text = _llm_response_text(llm_response)
    original_query = callback_context.state.get("original_query", "")
    if not text or not original_query:
        return
    payload = _parse_refine_result(text)
    if not payload.get("query"):
        return
    await llm_cache.exact_set(_REFINE_CACHE_NAMESPACE, original_query, payload)
    await llm_cache.semantic_set(_REFINE_CACHE_NAMESPACE, original_query, payload)


def _build_refine_agent() -> LlmAgent:
    def instruction(ctx: ReadonlyContext) -> str:
        return build_refine_query_prompt(ctx.state.get("original_query", ""))

    return LlmAgent(
        name="refine",
        # output_schema를 안 쓴다(2026-08-26, Qwen -> HCX 교체) - CLOVA
        # Studio의 OpenAI 호환 엔드포인트는 response_format을 json_schema/
        # json_object 둘 다 거부한다(실측 확인 - BadRequestError: "Invalid
        # parameter: response_format"). app/agents/hcx.py의 기존 게이트
        # (generate_query_variants)도 같은 이유로 response_format을 아예 안
        # 주고 프롬프트 지시 + parse_json_object로만 JSON을 뽑는다 - 여기도
        # 같은 패턴을 따른다(challenge와 동일한 원칙).
        model=LiteLlm(
            model=f"openai/{settings.hcx_model}",
            api_base=settings.hcx_api_base,
            api_key=settings.hcx_api_key,
            num_retries=0,
        ),
        instruction=instruction,
        output_key="refine_result",
        before_model_callback=[_skip_refine_if_already_specific, _refine_cache_lookup],
        after_model_callback=_refine_cache_store,
        on_model_error_callback=_on_refine_error,
    )


def _on_challenge_error(callback_context, llm_request, error) -> LlmResponse | None:
    """challenge(DeepSeek) 실패 시 "검증 없음"(빈 verdicts)으로 계속 진행한다
    - 전부 verified=None(미검증)으로 남을 뿐, judge/최종 추천 자체는 그대로
    진행된다(파이프라인 전체를 죽이지 않는다 - 모듈 docstring 참고)."""
    logger.warning("challenge 단계 모델 호출 실패 - 검증 없이 계속 진행", exc_info=error)
    return _model_error_fallback_response(_ChallengeResult(verdicts=[]).model_dump_json())


def _challenge_candidates(state: dict) -> list[dict]:
    return _ranked_items(state)[:_MAX_CHALLENGE_CANDIDATES]


async def _challenge_cache_lookup(callback_context, llm_request) -> LlmResponse | None:
    candidates = _challenge_candidates(callback_context.state)
    if not candidates:
        return None
    key = _pipeline_cache_key(_resolved_query(callback_context.state), candidates)
    cached = await llm_cache.exact_get(_CHALLENGE_CACHE_NAMESPACE, key)
    if cached is None:
        return None
    return _model_error_fallback_response(json.dumps(cached, ensure_ascii=False))


async def _challenge_cache_store(callback_context, llm_response: LlmResponse) -> None:
    """`_on_challenge_error`의 빈 verdicts 폴백은 캐시하지 않는다 - 그걸
    "검증해봤더니 문제없음"으로 저장해버리면, 이번엔 API가 일시적으로
    실패했을 뿐인데 다음 동일 질의/후보 조합에서도 영원히 검증을 건너뛰게
    된다(실패를 결론으로 착각하는 게 캐시 안 하는 것보다 나쁘다)."""
    candidates = _challenge_candidates(callback_context.state)
    if not candidates:
        return
    parsed = _parse_challenge_result(_llm_response_text(llm_response))
    if not parsed.get("verdicts"):
        return
    key = _pipeline_cache_key(_resolved_query(callback_context.state), candidates)
    await llm_cache.exact_set(_CHALLENGE_CACHE_NAMESPACE, key, parsed)


def _skip_challenge_always(callback_context, llm_request) -> LlmResponse | None:
    """6단계 challenge - raw 모드(2026-08-27, 사용자 요청 - "판단 없이 11번가
    상위로 뜨는거 그대로 가져오면 되잖아"). 의미 재검증 자체가 "판단"이므로
    LLM을 아예 호출하지 않고 항상 verdicts 빈 리스트를 흘려보낸다 - 뒤의
    apply_challenge/judge도 이 빈 결과를 받아 그대로 통과시킨다."""
    fallback = _ChallengeResult(verdicts=[])
    return _model_error_fallback_response(fallback.model_dump_json())


def _build_challenge_agent() -> LlmAgent:
    def instruction(ctx: ReadonlyContext) -> str:
        query = _resolved_query(ctx.state)
        candidates = _challenge_candidates(ctx.state)
        return _build_challenge_prompt(query, candidates)

    return LlmAgent(
        name="challenge",
        # output_schema를 안 쓴다 - DeepSeek이 그게 유발하는
        # response_format:json_schema(strict 구조화 출력)를 거부한다
        # (_parse_challenge_result 참고). response_format을 json_object로만
        # 요청해 다른 게이트들과 같은, DeepSeek이 실제로 지원하는 모드를 쓴다.
        model=LiteLlm(
            model=f"deepseek/{settings.deepseek_model}",
            num_retries=0,
            response_format={"type": "json_object"},
        ),
        instruction=instruction,
        output_key="challenge_result",
        before_model_callback=[_skip_challenge_always],
        after_model_callback=_challenge_cache_store,
        on_model_error_callback=_on_challenge_error,
    )


def _on_judge_error(callback_context, llm_request, error) -> LlmResponse | None:
    """judge(Qwen) 실패 시 index=-1 센티널을 흘려보낸다 - run()/run_stream()이
    이걸 "판단 없음"으로 읽어 `_build_decision(ranked, None)`의 기존 최저가
    규칙 폴백을 그대로 탄다(지금 `gpt.recommend_best` 실패 시 동작과 동일)."""
    logger.warning("judge 단계 모델 호출 실패 - 최저가 규칙 폴백으로 이어짐", exc_info=error)
    return _model_error_fallback_response(_JudgeVerdict(index=-1, reasoning="").model_dump_json())


def _skip_judge_always(callback_context, llm_request) -> LlmResponse | None:
    """8단계 judge - raw 모드(2026-08-27, 사용자 요청 - "판단 없이 11번가
    상위로 뜨는거 그대로 가져오면 되잖아"). "여러 후보 중 가장 나은 걸
    고른다"는 판단 자체를 하지 않고, 검색 결과 1위(index=0, 11번가
    ProductSearch가 추천도순으로 준 그대로)를 항상 그대로 확정한다."""
    fallback = _JudgeVerdict(index=0, reasoning="")
    return _model_error_fallback_response(fallback.model_dump_json())


def _judge_cache_key_extra(state: dict) -> str:
    """judge 캐시 키에 excluded_grade_tokens를 반영한다(2026-08-26 실측 -
    프롬프트에 "[다른 등급]" 표시를 추가하기 전 저장된 캐시가 그대로
    재사용돼, 프롬프트를 바꿔도 judge가 실제로는 재호출조차 안 되고
    옛(등급 미반영) 결과를 그대로 돌려주는 걸 확인했다. 후보 구성
    (candidate signature)이 같아도 등급 표시 유무에 따라 프롬프트 내용
    자체가 달라지니 캐시 키도 갈라야 한다."""
    tokens = state.get("excluded_grade_tokens") or []
    return "::grade=" + ",".join(sorted(tokens)) if tokens else ""


async def _judge_cache_lookup(callback_context, llm_request) -> LlmResponse | None:
    candidates = _ranked_items(callback_context.state)
    if not candidates:
        return None
    key = _pipeline_cache_key(
        _resolved_query(callback_context.state), candidates, _judge_cache_key_extra(callback_context.state)
    )
    cached = await llm_cache.exact_get(_JUDGE_CACHE_NAMESPACE, key)
    if cached is None:
        return None
    return _model_error_fallback_response(json.dumps(cached, ensure_ascii=False))


async def _judge_cache_store(callback_context, llm_response: LlmResponse) -> None:
    """`_on_judge_error`의 index=-1 센티널은 캐시하지 않는다 - challenge와
    같은 이유(_challenge_cache_store 참고): API 실패를 "판단 없음"이라는
    결론으로 굳혀버리면 안 된다."""
    candidates = _ranked_items(callback_context.state)
    if not candidates:
        return
    text = _llm_response_text(llm_response)
    try:
        parsed = parse_json_object(text)
    except (ValueError, TypeError):
        return
    if parsed.get("index", -1) == -1:
        return
    key = _pipeline_cache_key(
        _resolved_query(callback_context.state), candidates, _judge_cache_key_extra(callback_context.state)
    )
    await llm_cache.exact_set(_JUDGE_CACHE_NAMESPACE, key, parsed)


def _build_judge_agent() -> LlmAgent:
    def instruction(ctx: ReadonlyContext) -> str:
        query = _resolved_query(ctx.state)
        candidates = _candidates_with_verdicts(ctx.state)
        excluded_grade_tokens = ctx.state.get("excluded_grade_tokens") or []
        return build_recommend_prompt(query, candidates, excluded_grade_tokens)

    return LlmAgent(
        name="judge",
        model=LiteLlm(
            model=f"openai/{settings.qwen_model}",
            api_base=settings.qwen_api_base,
            api_key=settings.qwen_api_key,
            num_retries=0,
        ),
        instruction=instruction,
        output_schema=_JudgeVerdict,
        output_key="judge_result",
        before_model_callback=[_skip_judge_always],
        after_model_callback=_judge_cache_store,
        on_model_error_callback=_on_judge_error,
    )


def _build_pipeline() -> SequentialAgent:
    propose_parallel = ParallelAgent(name="propose", sub_agents=[_ElevenstProposeNode(name="elevenst")])

    return SequentialAgent(
        name="single_debate_pipeline",
        sub_agents=[
            _build_refine_agent(),
            _ElevenstSearchNode(name="search"),
            propose_parallel,
            _FilterMergeNode(name="filter_merge"),
            _DedupPagesNode(name="extract_pages"),
            _build_challenge_agent(),
            _ApplyChallengeNode(name="apply_challenge"),
            _build_judge_agent(),
        ],
    )


_runner: InMemoryRunner | None = None


def _get_runner() -> InMemoryRunner:
    global _runner
    if _runner is None:
        _runner = InMemoryRunner(agent=_build_pipeline(), app_name=_APP_NAME)
    return _runner


def _judge_recommended(state: dict) -> tuple[int, str] | None:
    """judge_result를 `_build_decision`이 기대하는 (index, reasoning) | None
    형태로 변환한다. index가 범위 밖이거나(모델이 지어낸 값)
    _on_judge_error의 -1 센티널이면 None - `_build_decision`이 이미 최저가
    규칙 폴백을 갖고 있다(지금 `gpt.recommend_best` 실패 시와 동일한
    계약)."""
    judge_result = state.get("judge_result")
    ranked = _ranked_items(state)
    if not judge_result or not ranked:
        return None
    index = judge_result.get("index", -1)
    if not (0 <= index < len(ranked)):
        return None
    return index, str(judge_result.get("reasoning") or "").strip()


def _judge_recommended_verified(state: dict, recommended: tuple[int, str] | None) -> bool | None:
    """judge가 실제로 고른 후보(recommended의 index)의 challenge verified
    값을 돌려준다(2026-08-27, 골든셋 실측 중 발견 - `_build_decision`이
    verified 인자 자체를 안 받아 schemas.Decision.verified가 문서와 달리
    항상 None으로 나오던 버그). judge가 실패했거나(recommended is None,
    최저가 폴백으로 이어짐) index가 proposals 범위 밖이면 None - 폴백
    선택은 challenge 검증 대상이 아니었으므로 애초에 verified를 판단할
    근거가 없다."""
    if recommended is None:
        return None
    index, _ = recommended
    proposals = state.get("proposals") or []
    if not (0 <= index < len(proposals)):
        return None
    return proposals[index].get("verified")


async def _run_pipeline_once(
    query: str,
    base_query: str | None,
    facet_answers: dict[str, list[str]] | None,
    force_price_rescue: bool,
) -> tuple[dict | None, str | None]:
    """파이프라인을 끝까지 한 번 돌리고 (최종 세션 state, 실패 메시지)를
    돌려준다 - 실패면 (None, 메시지), 성공이면 (state, None). run_stream이
    최초 1회 + 필요시 가격 보정 재시도 1회, 최대 2번 이 함수를 부른다.

    가격 표현은 refine(HCX) 전에 뗀다(2026-08-27, 사용자 리포트 -
    "200만원대 노트북 추천해줘"가 가격대와 무관한 액세서리를 줌 -> 원인은
    refine이 "200만원대"라는 어려운 표현까지 포함된 원문을 한 번에
    다듬으려다 비결정적으로 실패했기 때문이었다: 실측 시 "노트북 추천"
    으로 잘 나올 때도, "노트북"만 남기고 가격 조건 자체를 삭제할 때도
    있었다). `debate._search_and_rank_candidates`(레거시 경로)도 같은
    순서(가격 먼저 분리 -> 그 다음 정제)를 쓴다 - "200만원대"가 빠진
    "노트북 추천해줘"만 refine에 넘기면 짧고 단순해 훨씬 안정적으로
    다듬어진다."""
    query, price_min, price_max = extract_price_range(query)
    runner = _get_runner()
    session_id = str(uuid.uuid4())
    await runner.session_service.create_session(
        app_name=_APP_NAME,
        user_id="anonymous",
        session_id=session_id,
        state={
            "original_query": query,
            "base_query": base_query,
            "facet_answers": facet_answers,
            "force_price_rescue": force_price_rescue,
            "price_min": price_min,
            "price_max": price_max,
        },
    )

    gen = runner.run_async(
        user_id="anonymous",
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=query)]),
    )
    try:
        async for _event in gen:
            pass
    except RuntimeError as exc:
        return None, str(exc)
    except Exception:
        logger.exception("ADK 파이프라인 실행 실패: %r", query)
        return None, "구매 결정을 처리하는 중 오류가 발생했습니다."

    final_session = await runner.session_service.get_session(
        app_name=_APP_NAME, user_id="anonymous", session_id=session_id
    )
    return (dict(final_session.state) if final_session else {}), None


async def run_stream(
    query: str, base_query: str | None = None, facet_answers: dict[str, list[str]] | None = None
) -> AsyncIterator[dict[str, Any]]:
    """옛 `run_elevenst_only_debate_stream`(제거됨, 2026-08-26)이 쓰던 것과
    같은 이벤트 계약(status/final/error)을 유지한다 - 프론트는 그대로
    동작한다.

    raw 모드(2026-08-27, 사용자 요청 - "판단 없이 11번가 상위로 뜨는거
    그대로 가져오면 되잖아") - HCX 정제(refine) -> 11번가 검색(search,
    sortCd="A") -> 그 순서 그대로(filter_merge/challenge/judge 전부 판단
    없이 통과) 1위를 최종 추천으로 확정한다. 재검색·재시도 로직은 이미
    "결과를 보고 판단해서 다시 시도한다"는 개입이므로 두지 않는다 - 검색이
    빈 결과면 그대로 에러다."""
    yield {"type": "status", "stage": "searching"}

    state, pipeline_failed_message = await _run_pipeline_once(
        query, base_query, facet_answers, force_price_rescue=False
    )

    if pipeline_failed_message is not None:
        yield {"type": "error", "message": pipeline_failed_message}
        return

    ranked = _ranked_items(state)
    proposals_raw = state.get("proposals") or []
    if not ranked or not proposals_raw:
        yield {"type": "error", "message": f"11번가에서 '{query}'에 대해 관련성 있는 상품을 찾지 못했습니다."}
        return

    # 가격 조건에 맞는 후보가 하나도 없었던 경우(2026-08-27, filter_merge가
    # price_miss_note를 남김) - "가장 나은 걸 고른다"는 judge의 판단 자체가
    # 성립하지 않으므로, 가격 최근접순 1위(ranked[0])를 규칙 기반으로
    # 안내한다.
    price_miss_note = state.get("price_miss_note")
    if price_miss_note:
        decision = _build_price_miss_decision(ranked, price_miss_note)
    else:
        # raw 모드(2026-08-27) - judge를 항상 스킵하므로(_skip_judge_always)
        # judge_result는 언제나 index=0(11번가가 준 1위)이고, verified는
        # challenge를 스킵해 항상 None이다. `_build_decision`은 이 값을 그대로
        # 받아 판단 없이 후보 1위를 decision으로 확정한다.
        recommended = _judge_recommended(state)
        decision = _build_decision(
            ranked, recommended, model_label="elevenst", verified=_judge_recommended_verified(state, recommended)
        )
    result = DecideResponse(
        query=state.get("resolved_query", query),
        proposals=proposals_raw,
        decision=decision,
    )
    yield {"type": "final", "result": result.model_dump()}


async def run(
    query: str, base_query: str | None = None, facet_answers: dict[str, list[str]] | None = None
) -> DecideResponse:
    """`debate.run_elevenst_only_debate`와 정확히 같은 시그니처/반환 타입을
    유지한다."""
    result: DecideResponse | None = None
    error_message: str | None = None
    async for event in run_stream(query, base_query=base_query, facet_answers=facet_answers):
        if event["type"] == "final":
            result = DecideResponse.model_validate(event["result"])
        elif event["type"] == "error":
            error_message = event["message"]
    if result is None:
        raise RuntimeError(error_message or f"11번가에서 '{query}'에 대해 관련성 있는 상품을 찾지 못했습니다.")
    return result
