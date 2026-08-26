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

from google.adk.models import LlmResponse
from google.genai import types

from app import adk_pipeline, llm_cache
from fetchers.elevenst import ElevenstSearchItem


class _FakeCallbackContext:
    """실제 ADK CallbackContext 대신 쓰는 최소 스텁 - 캐시 콜백은
    `.state.get(...)`만 읽으므로 이 정도로 충분하다(모듈 docstring의 "ADK/
    LiteLlm 계층을 직접 목킹하지 않는다" 원칙과 별개 - 이건 ADK가 호출하는
    콜백 "함수"를 직접 단위 테스트하는 것이지 ADK 내부를 흉내내는 게 아니다)."""

    def __init__(self, state: dict):
        self.state = state


def _llm_response(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


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


def test_parse_challenge_result_parses_json_object_mode_response():
    raw = '{"verdicts": [{"index": 0, "verified": false, "note": "다른 상품"}]}'
    assert adk_pipeline._parse_challenge_result(raw) == {
        "verdicts": [{"index": 0, "verified": False, "note": "다른 상품"}]
    }


def test_parse_challenge_result_returns_empty_dict_on_malformed_text():
    assert adk_pipeline._parse_challenge_result("이건 JSON이 아님") == {}


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


def test_apply_challenge_verdicts_rule_based_override_catches_accessory_challenge_missed(monkeypatch):
    """2026-08-26, 사용자 리포트 - "1위에 휴대폰 뜨는데 2,3,4,5는 왜 저딴
    액세서리가 떠". challenge(DeepSeek)가 "핸드백 케이스"처럼 상품명에
    "케이스"가 그대로 있는 명백한 액세서리조차 verified=True로 통과시킨
    사례를 실측으로 확인 - 규칙 기반 안전망이 challenge 판정과 무관하게
    덮어써야 한다."""
    ranked = [_item("아이폰 17 mesh패턴 핸드백 케이스", 26600, code="1")]
    challenge_result = {"verdicts": [{"index": 0, "verified": True, "note": ""}]}

    proposals = adk_pipeline._apply_challenge_verdicts(ranked, challenge_result, query="아이폰 17")

    assert proposals[0]["verified"] is False
    assert "액세서리" in proposals[0]["challenge_note"]


def test_apply_challenge_verdicts_rule_based_override_skipped_when_query_wants_accessory():
    """"아이폰 케이스"를 검색했으면 케이스가 나오는 게 정상이므로 규칙 기반
    안전망이 개입하면 안 된다."""
    ranked = [_item("아이폰 17 실리콘 케이스", 9900, code="1")]
    proposals = adk_pipeline._apply_challenge_verdicts(ranked, {"verdicts": []}, query="아이폰 케이스")
    assert proposals[0]["verified"] is True


def test_apply_challenge_verdicts_does_not_override_when_challenge_already_flagged_false():
    """challenge가 이미 False로 판정한 건 규칙 기반 note로 덮어쓰지 않고
    challenge의 이유를 그대로 보존한다."""
    ranked = [_item("아이폰 17 케이스", 9900, code="1")]
    challenge_result = {"verdicts": [{"index": 0, "verified": False, "note": "본품이 아닌 액세서리"}]}

    proposals = adk_pipeline._apply_challenge_verdicts(ranked, challenge_result, query="아이폰 17")

    assert proposals[0]["verified"] is False
    assert proposals[0]["challenge_note"] == "본품이 아닌 액세서리"


def test_candidates_with_verdicts_overlays_verified_from_proposals():
    """2026-08-26, 사용자 리포트 - "골프공 검색했는데 골프파우치가 최종
    추천으로 뜸". judge 프롬프트용 후보에 challenge 검증 결과(verified/
    challenge_note)가 실려야 judge가 그 판정을 보고 피할 수 있다."""
    state = {
        "ranked_items": [_item("골프공 12개입", 15000, code="1"), _item("골프파우치", 9900, code="2")],
        "proposals": [
            {"verified": True, "challenge_note": None},
            {"verified": False, "challenge_note": "본품이 아닌 파우치"},
        ],
    }
    candidates = adk_pipeline._candidates_with_verdicts(state)
    assert candidates[0]["verified"] is True
    assert candidates[1]["verified"] is False
    assert candidates[1]["challenge_note"] == "본품이 아닌 파우치"


def test_candidates_with_verdicts_omits_verified_when_no_proposals_yet():
    state = {"ranked_items": [_item("A", 1000, code="1")], "proposals": []}
    candidates = adk_pipeline._candidates_with_verdicts(state)
    assert "verified" not in candidates[0]


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


def test_all_proposals_unverified_true_when_every_proposal_verified_false():
    proposals = [{"verified": False}, {"verified": False}]
    assert adk_pipeline._all_proposals_unverified(proposals) is True


def test_all_proposals_unverified_false_when_one_verified_true():
    proposals = [{"verified": False}, {"verified": True}]
    assert adk_pipeline._all_proposals_unverified(proposals) is False


def test_all_proposals_unverified_false_when_unchecked():
    """verified=None(challenge가 아예 안 다룬 후보, 검증 안 됨)은
    verified=False(검증해봤는데 아니라고 판정됨)와 다르다 - 재시도 트리거가
    아니다."""
    assert adk_pipeline._all_proposals_unverified([{"verified": None}]) is False


def test_all_proposals_unverified_false_for_empty_list():
    assert adk_pipeline._all_proposals_unverified([]) is False


# ---------------------------------------------------------------------------
# run()/run_stream() 통합 - 실제 SequentialAgent 오케스트레이션을 그대로
# 태우되, 네트워크가 필요한 지점만 몽키패치한다.
# ---------------------------------------------------------------------------


def test_run_stream_raises_runtime_error_when_no_relevant_candidates(monkeypatch):
    async def _empty_search(query, base_query, facet_answers, force_price_rescue=False):
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
    async def _empty_search(query, base_query, facet_answers, force_price_rescue=False):
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


# ---------------------------------------------------------------------------
# refine/challenge/judge 캐시 콜백(2026-08-26) - llm_cache.exact_get/set을
# 인메모리 dict로 몽키패치해 실제 Supabase 없이 콜백 로직만 검증한다.
# ---------------------------------------------------------------------------


def test_candidate_cache_signature_differs_when_price_changes():
    a = [_item("A", 1000, code="1")]
    b = [_item("A", 1200, code="1")]
    assert adk_pipeline._candidate_cache_signature(a) != adk_pipeline._candidate_cache_signature(b)


def test_llm_response_text_joins_text_parts():
    assert adk_pipeline._llm_response_text(_llm_response('{"query": "나이키"}')) == '{"query": "나이키"}'


def test_refine_cache_lookup_returns_none_on_miss(monkeypatch):
    async def _miss(namespace, query):
        return None

    monkeypatch.setattr(llm_cache, "exact_get", _miss)
    monkeypatch.setattr(llm_cache, "semantic_get", _miss)

    ctx = _FakeCallbackContext({"original_query": "나이키 신발 사고싶어"})
    result = asyncio.run(adk_pipeline._refine_cache_lookup(ctx, llm_request=None))
    assert result is None


def test_refine_cache_lookup_returns_cached_response_on_hit(monkeypatch):
    async def _hit(namespace, query):
        assert query == "나이키 신발 사고싶어"
        return {"query": "나이키 신발"}

    monkeypatch.setattr(llm_cache, "exact_get", _hit)

    ctx = _FakeCallbackContext({"original_query": "나이키 신발 사고싶어"})
    result = asyncio.run(adk_pipeline._refine_cache_lookup(ctx, llm_request=None))
    assert result is not None
    assert adk_pipeline._llm_response_text(result) == '{"query": "나이키 신발"}'


def test_refine_cache_store_saves_parsed_result(monkeypatch):
    saved = {}

    async def _exact_set(namespace, query, response):
        saved["exact"] = (namespace, query, response)

    async def _semantic_set(namespace, query, response):
        saved["semantic"] = (namespace, query, response)

    monkeypatch.setattr(llm_cache, "exact_set", _exact_set)
    monkeypatch.setattr(llm_cache, "semantic_set", _semantic_set)

    ctx = _FakeCallbackContext({"original_query": "나이키 신발 사고싶어"})
    asyncio.run(
        adk_pipeline._refine_cache_store(ctx, _llm_response('{"query": "나이키 신발"}'))
    )

    namespace, query, response = saved["exact"]
    assert (namespace, query) == (adk_pipeline._REFINE_CACHE_NAMESPACE, "나이키 신발 사고싶어")
    assert response["query"] == "나이키 신발"
    assert saved["semantic"][2]["query"] == "나이키 신발"


def test_challenge_cache_lookup_keys_on_query_and_candidates(monkeypatch):
    seen_key = {}

    async def _hit(namespace, query):
        seen_key["namespace"] = namespace
        seen_key["query"] = query
        return {"verdicts": [{"index": 0, "verified": False, "note": "다른 상품"}]}

    monkeypatch.setattr(llm_cache, "exact_get", _hit)

    ranked = [_item("나이키 카드지갑", 14400, code="1")]
    ctx = _FakeCallbackContext({"original_query": "나이키 에어포스1", "ranked_items": ranked})
    result = asyncio.run(adk_pipeline._challenge_cache_lookup(ctx, llm_request=None))

    assert seen_key["namespace"] == adk_pipeline._CHALLENGE_CACHE_NAMESPACE
    assert seen_key["query"] == adk_pipeline._pipeline_cache_key("나이키 에어포스1", ranked)
    assert result is not None


def test_challenge_cache_lookup_returns_none_when_no_candidates(monkeypatch):
    async def _boom(namespace, query):
        raise AssertionError("후보가 없는데 캐시를 조회했다")

    monkeypatch.setattr(llm_cache, "exact_get", _boom)

    ctx = _FakeCallbackContext({"original_query": "나이키 에어포스1", "ranked_items": []})
    assert asyncio.run(adk_pipeline._challenge_cache_lookup(ctx, llm_request=None)) is None


def test_challenge_cache_store_skips_empty_verdicts_fallback(monkeypatch):
    """_on_challenge_error가 합성한 빈 verdicts 폴백은 캐시하면 안 된다 -
    API 일시 실패를 "검증해봤더니 문제없음"으로 영구 저장하게 된다."""

    async def _boom(namespace, query, response):
        raise AssertionError("실패 폴백을 캐시에 저장했다")

    monkeypatch.setattr(llm_cache, "exact_set", _boom)

    ranked = [_item("나이키 카드지갑", 14400, code="1")]
    ctx = _FakeCallbackContext({"original_query": "나이키 에어포스1", "ranked_items": ranked})
    asyncio.run(adk_pipeline._challenge_cache_store(ctx, _llm_response('{"verdicts": []}')))


def test_challenge_cache_store_saves_real_verdicts(monkeypatch):
    saved = {}

    async def _exact_set(namespace, query, response):
        saved["call"] = (namespace, query, response)

    monkeypatch.setattr(llm_cache, "exact_set", _exact_set)

    ranked = [_item("나이키 카드지갑", 14400, code="1")]
    ctx = _FakeCallbackContext({"original_query": "나이키 에어포스1", "ranked_items": ranked})
    asyncio.run(
        adk_pipeline._challenge_cache_store(
            ctx, _llm_response('{"verdicts": [{"index": 0, "verified": false, "note": "액세서리"}]}')
        )
    )

    assert saved["call"][0] == adk_pipeline._CHALLENGE_CACHE_NAMESPACE
    assert saved["call"][2] == {"verdicts": [{"index": 0, "verified": False, "note": "액세서리"}]}


def test_judge_cache_store_skips_sentinel_index(monkeypatch):
    """_on_judge_error가 합성한 index=-1 센티널은 캐시하면 안 된다."""

    async def _boom(namespace, query, response):
        raise AssertionError("판단 없음 센티널을 캐시에 저장했다")

    monkeypatch.setattr(llm_cache, "exact_set", _boom)

    ranked = [_item("나이키 에어포스1", 129000, code="1")]
    ctx = _FakeCallbackContext({"original_query": "나이키 에어포스1", "ranked_items": ranked})
    asyncio.run(adk_pipeline._judge_cache_store(ctx, _llm_response('{"index": -1, "reasoning": ""}')))


def test_judge_cache_store_saves_real_pick(monkeypatch):
    saved = {}

    async def _exact_set(namespace, query, response):
        saved["call"] = (namespace, query, response)

    monkeypatch.setattr(llm_cache, "exact_set", _exact_set)

    ranked = [_item("나이키 에어포스1", 129000, code="1")]
    ctx = _FakeCallbackContext({"original_query": "나이키 에어포스1", "ranked_items": ranked})
    asyncio.run(
        adk_pipeline._judge_cache_store(ctx, _llm_response('{"index": 0, "reasoning": "가성비 좋음"}'))
    )

    assert saved["call"][0] == adk_pipeline._JUDGE_CACHE_NAMESPACE
    assert saved["call"][2] == {"index": 0, "reasoning": "가성비 좋음"}


def test_judge_cache_lookup_returns_cached_index_on_hit(monkeypatch):
    async def _hit(namespace, query):
        return {"index": 0, "reasoning": "가성비 좋음"}

    monkeypatch.setattr(llm_cache, "exact_get", _hit)

    ranked = [_item("나이키 에어포스1", 129000, code="1")]
    ctx = _FakeCallbackContext({"original_query": "나이키 에어포스1", "ranked_items": ranked})
    result = asyncio.run(adk_pipeline._judge_cache_lookup(ctx, llm_request=None))

    assert result is not None
    assert adk_pipeline._llm_response_text(result) == '{"index": 0, "reasoning": "가성비 좋음"}'
