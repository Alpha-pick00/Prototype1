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
"""

from __future__ import annotations

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

from . import price_table as price_table_module
from .agents.base import build_recommend_prompt, build_refine_query_prompt
from .config import settings
from .debate import (
    _build_decision,
    _build_proposals,
    _rank_by_relevance,
    _refine_base_query,
    _search_candidates,
    _search_with_query_variants,
)
from .intent import looks_conversational_query
from .schemas import DecideResponse, RefinedQuery

logger = logging.getLogger(__name__)

_APP_NAME = "alpha_pick_adk_pipeline"

# challenge(DeepSeek 의미 재검증)는 이번 재구성에서 새로 생긴 추가 LLM
# 호출이라 비용을 상위 몇 개로 제한한다(gpt.candidate_notes의
# _MAX_CANDIDATE_NOTES=5, 옛 파이프라인의 _MAX_EXTRACT_CANDIDATES=10과 같은
# 원칙 - "상위 후보만 추가로 신경 쓴다").
_MAX_CHALLENGE_CANDIDATES = 10


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


def _resolved_query(state: dict) -> str:
    """refine 단계 결과(refine_result.query)가 있으면 그걸, 없으면(스킵/실패)
    원본 질의를 쓴다."""
    refine_result = state.get("refine_result") or {}
    return refine_result.get("query") or state.get("original_query") or ""


def _ranked_items(state: dict) -> list[elevenst.ElevenstSearchItem]:
    return state.get("ranked_items") or []


def _model_error_fallback_response(text: str) -> LlmResponse:
    """모델 호출이 실패했을 때 성공한 것처럼 대신 흘려보낼 최소 응답 - ADK가 이
    텍스트를 실제 모델 응답과 동일하게 output_key/output_schema 경로로 흘려
    보낸다(옛 adk_pipeline.py와 동일한 패턴)."""
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


class _ElevenstSearchNode(BaseAgent):
    """2단계 search - 11번가 ProductSearch로 검색한다(`debate._search_candidates`
    재사용, base_query/facet_answers 기반 드릴다운 로컬 필터링도 그대로 적용).
    base_query는 refine 결과와 별개로 `_refine_base_query`로 일관되게 정제한다
    (2026-08-25 회귀 수정과 같은 이유 - query만 정제되고 base_query가 안
    맞으면 drilldown 필터가 유효 후보를 전부 걸러낸다)."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        original_query = state.get("original_query", "")
        query = _resolved_query(state)
        base_query = await _refine_base_query(state.get("base_query"), original_query, query)
        facet_answers = state.get("facet_answers")

        items = await _search_candidates(query, base_query, facet_answers)

        yield Event(
            author=self.name,
            actions=EventActions(
                state_delta={
                    "search_items": items,
                    "resolved_query": query,
                    "resolved_base_query": base_query,
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
    """4단계 filter_merge - 관련성 필터(`_product_name_matches`) -> 0건이면
    임베딩 의미 유사도 구제(`semantic_relevance_fallback`, facet_answers가
    있을 때는 건너뜀 - 이미 구조적으로 필터링된 표본이라) -> 그래도 0건이면
    HCX 표기 변형 재검색(`_search_with_query_variants`) -> 관련도순 임베딩
    랭킹(`_rank_by_relevance`) 순서로 `debate.py`의
    `_search_and_rank_candidates`와 동일한 로직을 그대로 재사용한다. 끝까지
    관련 상품을 못 찾으면 RuntimeError를 그대로 던진다 - SequentialAgent는
    서브 에이전트 예외를 그대로 전체 실패로 전파하고, `run()`/`run_stream()`의
    바깥 try/except가 이를 "error" 이벤트로 옮긴다(옛 파이프라인과 동일한
    전파 방식)."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        query = state.get("resolved_query", "")
        facet_answers = state.get("facet_answers")
        items = state.get("candidates") or []

        relevant = (
            items
            if facet_answers
            else [it for it in items if price_table_module._product_name_matches(query, it["product_name"])]
        )
        if not relevant and not facet_answers:
            relevant = await price_table_module.semantic_relevance_fallback(query, items)
        if not relevant:
            relevant = await _search_with_query_variants(query)
        if not relevant:
            raise RuntimeError(f"11번가에서 '{query}'에 대해 관련성 있는 상품을 찾지 못했습니다.")

        ranked = await _rank_by_relevance(query, relevant)
        yield Event(author=self.name, actions=EventActions(state_delta={"ranked_items": ranked}))


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


def _apply_challenge_verdicts(
    ranked: list[elevenst.ElevenstSearchItem], challenge_result: dict
) -> list[dict]:
    """7단계 apply_challenge의 핵심 로직(순수 함수) - challenge(DeepSeek)
    검증 결과를 `_build_proposals`가 만든 Proposal 목록에 index 기준으로
    덧씌운다(`verified`/`challenge_note`). challenge가 다루지 않은(상위
    _MAX_CHALLENGE_CANDIDATES 밖) 후보는 verified=None(미검증)으로 남는다 -
    "검증 안 됨"이지 "검증 실패"가 아니므로 judge 단계에서 배제하지 않는다.
    ADK Event/state_delta는 dict만 담을 수 있어 Proposal.model_dump() 결과를
    반환한다."""
    verdicts_by_index = {v["index"]: v for v in challenge_result.get("verdicts") or []}

    proposals = _build_proposals(ranked, {})
    updated = []
    for i, proposal in enumerate(proposals):
        verdict = verdicts_by_index.get(i)
        if verdict is None:
            updated.append(proposal)
            continue
        updated.append(
            proposal.model_copy(update={"verified": verdict["verified"], "challenge_note": verdict.get("note") or None})
        )
    return [p.model_dump() for p in updated]


class _ApplyChallengeNode(BaseAgent):
    """7단계 apply_challenge - 로직은 `_apply_challenge_verdicts`(순수 함수)에
    있고, 이 노드는 세션 상태를 읽고/쓰는 얇은 ADK 래퍼일 뿐이다."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        ranked = ctx.session.state.get("ranked_items") or []
        challenge_result = ctx.session.state.get("challenge_result") or {}
        proposals = _apply_challenge_verdicts(ranked, challenge_result)
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


def _build_refine_agent() -> LlmAgent:
    def instruction(ctx: ReadonlyContext) -> str:
        return build_refine_query_prompt(ctx.state.get("original_query", ""))

    return LlmAgent(
        name="refine",
        model=LiteLlm(
            model=f"openai/{settings.qwen_model}",
            api_base=settings.qwen_api_base,
            api_key=settings.qwen_api_key,
            num_retries=0,
        ),
        instruction=instruction,
        output_schema=RefinedQuery,
        output_key="refine_result",
        before_model_callback=_skip_refine_if_already_specific,
        on_model_error_callback=_on_refine_error,
    )


def _on_challenge_error(callback_context, llm_request, error) -> LlmResponse | None:
    """challenge(DeepSeek) 실패 시 "검증 없음"(빈 verdicts)으로 계속 진행한다
    - 전부 verified=None(미검증)으로 남을 뿐, judge/최종 추천 자체는 그대로
    진행된다(파이프라인 전체를 죽이지 않는다 - 모듈 docstring 참고)."""
    logger.warning("challenge 단계 모델 호출 실패 - 검증 없이 계속 진행", exc_info=error)
    return _model_error_fallback_response(_ChallengeResult(verdicts=[]).model_dump_json())


def _build_challenge_agent() -> LlmAgent:
    def instruction(ctx: ReadonlyContext) -> str:
        query = _resolved_query(ctx.state)
        candidates = _ranked_items(ctx.state)[:_MAX_CHALLENGE_CANDIDATES]
        return _build_challenge_prompt(query, candidates)

    return LlmAgent(
        name="challenge",
        model=LiteLlm(model=f"deepseek/{settings.deepseek_model}", num_retries=0),
        instruction=instruction,
        output_schema=_ChallengeResult,
        output_key="challenge_result",
        on_model_error_callback=_on_challenge_error,
    )


def _on_judge_error(callback_context, llm_request, error) -> LlmResponse | None:
    """judge(Qwen) 실패 시 index=-1 센티널을 흘려보낸다 - run()/run_stream()이
    이걸 "판단 없음"으로 읽어 `_build_decision(ranked, None)`의 기존 최저가
    규칙 폴백을 그대로 탄다(지금 `gpt.recommend_best` 실패 시 동작과 동일)."""
    logger.warning("judge 단계 모델 호출 실패 - 최저가 규칙 폴백으로 이어짐", exc_info=error)
    return _model_error_fallback_response(_JudgeVerdict(index=-1, reasoning="").model_dump_json())


def _build_judge_agent() -> LlmAgent:
    def instruction(ctx: ReadonlyContext) -> str:
        query = _resolved_query(ctx.state)
        candidates = _ranked_items(ctx.state)
        return build_recommend_prompt(query, candidates)

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


async def run_stream(
    query: str, base_query: str | None = None, facet_answers: dict[str, list[str]] | None = None
) -> AsyncIterator[dict[str, Any]]:
    """`debate.run_elevenst_only_debate_stream`과 정확히 같은 이벤트 계약
    (status/final/error)을 유지한다 - main.py가 어느 구현을 부르는지와
    무관하게 프론트는 그대로 동작한다."""
    runner = _get_runner()
    session_id = str(uuid.uuid4())
    await runner.session_service.create_session(
        app_name=_APP_NAME,
        user_id="anonymous",
        session_id=session_id,
        state={"original_query": query, "base_query": base_query, "facet_answers": facet_answers},
    )

    yield {"type": "status", "stage": "searching"}

    pipeline_failed_message: str | None = None
    gen = runner.run_async(
        user_id="anonymous",
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=query)]),
    )
    try:
        async for _event in gen:
            pass
    except RuntimeError as exc:
        pipeline_failed_message = str(exc)
    except Exception:
        logger.exception("ADK 파이프라인 실행 실패: %r", query)
        pipeline_failed_message = "구매 결정을 처리하는 중 오류가 발생했습니다."

    if pipeline_failed_message is not None:
        yield {"type": "error", "message": pipeline_failed_message}
        return

    final_session = await runner.session_service.get_session(
        app_name=_APP_NAME, user_id="anonymous", session_id=session_id
    )
    state: dict = dict(final_session.state) if final_session else {}

    ranked = _ranked_items(state)
    proposals_raw = state.get("proposals") or []
    if not ranked or not proposals_raw:
        yield {"type": "error", "message": f"11번가에서 '{query}'에 대해 관련성 있는 상품을 찾지 못했습니다."}
        return

    decision = _build_decision(ranked, _judge_recommended(state))
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
