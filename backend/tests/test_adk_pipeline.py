"""ADK 8단계 SequentialAgent 파이프라인(app.adk_pipeline) 테스트.

옛 test_adk_pipeline.py(2026-08-20 삭제 전, git 이력에서 확인)도 ADK/LiteLlm
계층을 직접 목킹하지 않고 순수 함수(_build_decision, _apply_challenge 등)만
단위 테스트했다 - 이 파일도 같은 원칙을 따른다. LLM을 실제로 부르는 것은
challenge/judge뿐이고 둘 다 ADK LlmAgent + LiteLlm로 감싸져 있어, "성공
응답"을 목으로 흉내내려면 litellm 내부 응답 객체 모양에 의존하게 돼 깨지기
쉽다 - 대신 실패/폴백 경로(0건 -> RuntimeError)는 refine을 건너뛰게 만드는
비대화체 질의 + 검색 결과 0건 몽키패치로 실제 SequentialAgent 오케스트레이션을
그대로 태워 검증한다(네트워크 요청 없음 - refine은 looks_conversational_query가
False라 before_model_callback이 LLM 호출 자체를 건너뛰고, filter_merge가
관련 후보를 못 찾으면 challenge/judge에 도달하기 전에 RuntimeError를 던진다).

네트워크 요청 금지 - 전부 monkeypatch."""

from __future__ import annotations

import asyncio

from app import adk_pipeline
from fetchers.elevenst import ElevenstSearchItem


def _item(
    name: str,
    price: int,
    code: str = "1",
    url: str | None = None,
) -> ElevenstSearchItem:
    return ElevenstSearchItem(
        product_code=code,
        product_name=name,
        price_krw=price,
        seller="판매자",
        url=url or f"https://www.11st.co.kr/products/{code}",
        review_count=None,
        buy_satisfy=None,
        image_url=None,
    )


# ---------------------------------------------------------------------------
# 순수 함수 단위 테스트
# ---------------------------------------------------------------------------


def test_dedupe_items_removes_duplicate_product_codes_keeping_first():
    items = [_item("A", 1000, code="1"), _item("A 중복", 1200, code="1"), _item("B", 2000, code="2")]
    deduped = adk_pipeline._dedupe_items(items)
    assert [it["product_code"] for it in deduped] == ["1", "2"]
    assert deduped[0]["product_name"] == "A"  # 먼저 나온(더 관련도 높은) 쪽을 유지


def test_dedupe_items_falls_back_to_url_when_product_code_missing():
    a = _item("A", 1000, code="", url="https://example.com/a")
    b = _item("A 복사본", 1000, code="", url="https://example.com/a")
    deduped = adk_pipeline._dedupe_items([a, b])
    assert len(deduped) == 1


def test_apply_challenge_verdicts_overlays_verified_and_note_by_index():
    ranked = [_item("A", 1000, code="1"), _item("B", 2000, code="2")]
    challenge_result = {"verdicts": [{"index": 1, "verified": False, "note": "다른 상품으로 의심됨"}]}

    proposals = adk_pipeline._apply_challenge_verdicts(ranked, challenge_result)

    assert proposals[0]["verified"] is True  # challenge가 안 건드린 index 0은 _build_proposals 기본값 유지
    assert proposals[1]["verified"] is False
    assert proposals[1]["challenge_note"] == "다른 상품으로 의심됨"


def test_apply_challenge_verdicts_leaves_uncovered_candidates_untouched():
    """challenge가 상위 N개만 다뤄서 verdicts에 없는 index는 _build_proposals의
    기본 verified=True를 그대로 유지한다(검증 안 됨 != 검증 실패)."""
    ranked = [_item("A", 1000, code="1")]
    proposals = adk_pipeline._apply_challenge_verdicts(ranked, {"verdicts": []})
    assert proposals[0]["verified"] is True
    assert proposals[0]["challenge_note"] is None


def test_resolved_query_prefers_refine_result_over_original():
    state = {"original_query": "원본", "refine_result": {"query": "정제됨"}}
    assert adk_pipeline._resolved_query(state) == "정제됨"


def test_resolved_query_falls_back_to_original_when_refine_missing():
    state = {"original_query": "원본"}
    assert adk_pipeline._resolved_query(state) == "원본"


def test_judge_recommended_returns_none_for_sentinel_index():
    state = {"judge_result": {"index": -1, "reasoning": ""}, "ranked_items": [_item("A", 1000)]}
    assert adk_pipeline._judge_recommended(state) is None


def test_judge_recommended_returns_none_when_index_out_of_range():
    state = {"judge_result": {"index": 5, "reasoning": "x"}, "ranked_items": [_item("A", 1000)]}
    assert adk_pipeline._judge_recommended(state) is None


def test_judge_recommended_returns_index_and_reasoning_when_valid():
    state = {"judge_result": {"index": 0, "reasoning": "가성비 좋음"}, "ranked_items": [_item("A", 1000)]}
    assert adk_pipeline._judge_recommended(state) == (0, "가성비 좋음")


def test_build_challenge_prompt_includes_query_and_indexed_candidates():
    prompt = adk_pipeline._build_challenge_prompt("나이키", [_item("나이키 에어포스1", 129000, code="1")])
    assert "나이키" in prompt
    assert "[0] 나이키 에어포스1" in prompt
    assert "129,000원" in prompt


# ---------------------------------------------------------------------------
# run()/run_stream() 통합 - 실제 SequentialAgent 오케스트레이션을 그대로
# 태우되, 네트워크가 필요한 지점만 몽키패치한다.
# ---------------------------------------------------------------------------


def test_run_stream_raises_runtime_error_when_no_relevant_candidates(monkeypatch):
    async def _empty_search(query, base_query, facet_answers):
        return []

    monkeypatch.setattr(adk_pipeline, "_search_candidates", _empty_search)

    async def _empty_variants(query):
        return []

    from app.agents import hcx

    monkeypatch.setattr(hcx, "generate_query_variants", _empty_variants)

    async def _collect():
        # 비대화체 질의라 looks_conversational_query가 False -> refine의
        # before_model_callback이 LLM 호출 없이 즉시 스킵한다.
        return [e async for e in adk_pipeline.run_stream("존재하지않는이상한상품쿼리123")]

    events = asyncio.run(_collect())

    assert events[0] == {"type": "status", "stage": "searching"}
    assert events[-1]["type"] == "error"
    assert "관련성 있는 상품을 찾지 못했습니다" in events[-1]["message"]


def test_run_raises_runtime_error_when_no_relevant_candidates(monkeypatch):
    async def _empty_search(query, base_query, facet_answers):
        return []

    monkeypatch.setattr(adk_pipeline, "_search_candidates", _empty_search)

    from app.agents import hcx

    async def _empty_variants(query):
        return []

    monkeypatch.setattr(hcx, "generate_query_variants", _empty_variants)

    try:
        asyncio.run(adk_pipeline.run("존재하지않는이상한상품쿼리123"))
        raise AssertionError("RuntimeError가 발생해야 한다")
    except RuntimeError as exc:
        assert "관련성 있는 상품을 찾지 못했습니다" in str(exc)
