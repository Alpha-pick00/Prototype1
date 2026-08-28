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


def test_apply_challenge_verdicts_ignores_verdicts_in_raw_mode(monkeypatch):
    """raw 모드(2026-08-27, 사용자 요청 - "판단 없이 그대로") - challenge
    자체가 항상 스킵되므로 verdicts에 값이 들어와도 무시하고 _build_proposals
    기본값(verified=True)을 그대로 반환한다."""

    async def _no_notes(query, candidates):
        return {}

    monkeypatch.setattr(adk_pipeline.gpt, "candidate_notes", _no_notes)

    ranked = [_item("A", 1000, code="1"), _item("B", 2000, code="2")]
    challenge_result = {"verdicts": [{"index": 1, "verified": False, "note": "다른 상품으로 의심됨"}]}

    proposals = asyncio.run(adk_pipeline._apply_challenge_verdicts(ranked, challenge_result))

    assert proposals[0]["verified"] is True
    assert proposals[1]["verified"] is True
    assert proposals[1]["challenge_note"] is None


def test_apply_challenge_verdicts_does_not_flag_accessory_looking_names_in_raw_mode(monkeypatch):
    """raw 모드에서는 상품명에 "케이스" 같은 액세서리 지시어가 있어도 규칙
    기반으로 verified=False를 붙이지 않는다(2026-08-27, 사용자 요청 - "판단
    없이 그대로 가져오면 되잖아")."""

    async def _no_notes(query, candidates):
        return {}

    monkeypatch.setattr(adk_pipeline.gpt, "candidate_notes", _no_notes)

    ranked = [_item("아이폰 17 mesh패턴 핸드백 케이스", 26600, code="1")]
    proposals = asyncio.run(adk_pipeline._apply_challenge_verdicts(ranked, {"verdicts": []}, query="아이폰 17"))
    assert proposals[0]["verified"] is True


def test_apply_challenge_verdicts_attaches_candidate_notes(monkeypatch):
    """각 후보별 추천 이유(2026-08-27, 사용자 요청 - "각 후보군별로 추천
    이유도 설명해줘 너가 reasoning 해서") - gpt.candidate_notes가 돌려준
    문장이 index 순서 그대로 proposals의 reasoning에 반영된다. 순서
    자체는 challenge_result와 무관하게 ranked 그대로 유지된다."""

    async def _notes(query, candidates):
        assert query == "나이키 에어포스1"
        return {0: "가장 인기 있는 컬러입니다.", 1: "가성비가 좋습니다."}

    monkeypatch.setattr(adk_pipeline.gpt, "candidate_notes", _notes)

    ranked = [_item("나이키 에어포스1 화이트", 129000, code="1"), _item("나이키 에어포스1 블랙", 119000, code="2")]
    proposals = asyncio.run(adk_pipeline._apply_challenge_verdicts(ranked, {"verdicts": []}, query="나이키 에어포스1"))

    assert proposals[0]["reasoning"] == "가장 인기 있는 컬러입니다."
    assert proposals[1]["reasoning"] == "가성비가 좋습니다."


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


def test_resolved_query_parses_raw_text_refine_result():
    """2026-08-26, refine을 Qwen -> HCX로 교체 - HCX는 response_format을
    전혀 지원하지 않아 output_schema를 뺐다. output_schema 없이는 ADK가
    refine_result를 dict로 파싱해주지 않고 원문 텍스트 그대로 state에
    남긴다 - _resolved_query가 직접 파싱해야 한다."""
    state = {"original_query": "원본", "refine_result": '{"query": "정제됨", "error": null}'}
    assert adk_pipeline._resolved_query(state) == "정제됨"


def test_resolved_query_falls_back_to_original_when_refine_result_text_malformed():
    state = {"original_query": "원본", "refine_result": "이건 JSON이 아님"}
    assert adk_pipeline._resolved_query(state) == "원본"


def test_resolved_query_parses_bare_json_string_literal_refine_result():
    """실측(2026-08-26, HCX 전환 후) - 프롬프트가 `{"query": "..."}`를
    지시해도 HCX가 그냥 `"저렴한 아기 간식"`(JSON 객체가 아니라 순수 문자열
    리터럴)만 돌려준 사례가 있었다 - 이 경우도 정제 결과로 인정해야 한다."""
    state = {"original_query": "저렴한 아기 간식을 사고 싶어", "refine_result": '"저렴한 아기 간식"'}
    assert adk_pipeline._resolved_query(state) == "저렴한 아기 간식"


def test_judge_recommended_returns_none_for_sentinel_index():
    state = {"judge_result": {"index": -1, "reasoning": ""}, "ranked_items": [_item("A", 1000)]}
    assert adk_pipeline._judge_recommended(state) is None


def test_judge_recommended_returns_none_when_index_out_of_range():
    state = {"judge_result": {"index": 5, "reasoning": "x"}, "ranked_items": [_item("A", 1000)]}
    assert adk_pipeline._judge_recommended(state) is None


def test_judge_recommended_returns_index_and_reasoning_when_valid():
    state = {"judge_result": {"index": 0, "reasoning": "가성비 좋음"}, "ranked_items": [_item("A", 1000)]}
    assert adk_pipeline._judge_recommended(state) == (0, "가성비 좋음")


# ---------------------------------------------------------------------------
# _judge_recommended_verified - Decision.verified가 항상 None으로 나오던
# 버그 수정(2026-08-27, 골든셋 50개 실측 중 발견 - schemas.Decision.verified
# docstring은 "challenge가 필요 없는 경우도 True로 강제되므로 None에는 안
# 걸림"이라고 명시하는데, _build_decision이 verified 인자 자체를 안 받아
# 실제로는 골든셋 43건 전량이 None으로 나왔다).
# ---------------------------------------------------------------------------


def test_judge_recommended_verified_returns_challenge_verdict_for_chosen_index():
    state = {"proposals": [{"verified": True}, {"verified": False}]}
    assert adk_pipeline._judge_recommended_verified(state, (1, "이유")) is False
    assert adk_pipeline._judge_recommended_verified(state, (0, "이유")) is True


def test_judge_recommended_verified_returns_none_when_judge_failed():
    state = {"proposals": [{"verified": True}]}
    assert adk_pipeline._judge_recommended_verified(state, None) is None


def test_judge_recommended_verified_returns_none_when_index_out_of_range():
    state = {"proposals": [{"verified": True}]}
    assert adk_pipeline._judge_recommended_verified(state, (5, "이유")) is None


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
    async def _empty_search(query, limit):
        return []

    monkeypatch.setattr(adk_pipeline.elevenst, "search_elevenst_web_ranking", _empty_search)

    async def _collect():
        # 비대화체 질의라 looks_conversational_query가 False -> refine의
        # before_model_callback이 LLM 호출 없이 즉시 스킵한다.
        return [e async for e in adk_pipeline.run_stream("존재하지않는이상한상품쿼리123")]

    events = asyncio.run(_collect())

    assert events[0] == {"type": "status", "stage": "searching"}
    assert events[-1]["type"] == "error"
    assert "검색 결과를 찾지 못했습니다" in events[-1]["message"]


def test_run_raises_runtime_error_when_no_relevant_candidates(monkeypatch):
    async def _empty_search(query, limit):
        return []

    monkeypatch.setattr(adk_pipeline.elevenst, "search_elevenst_web_ranking", _empty_search)

    try:
        asyncio.run(adk_pipeline.run("존재하지않는이상한상품쿼리123"))
        raise AssertionError("RuntimeError가 발생해야 한다")
    except RuntimeError as exc:
        assert "검색 결과를 찾지 못했습니다" in str(exc)


def test_run_skips_challenge_and_judge_llm_calls_end_to_end(monkeypatch):
    """raw 모드(2026-08-27, 사용자 요청 - "판단 없이 11번가 상위로 뜨는거
    그대로 가져오면 되잖아") - challenge/judge LLM(DeepSeek/Qwen)이 실제로
    호출되지 않고, 관련성 필터를 통과한 11번가 검색 결과 1위가 그대로 최종
    결정이 돼야 한다(2026-08-27, 사용자 리포트 - "11번가에 아이폰 17
    검색하면 핸드폰으로 매핑되는데 왜 API 통해서 하면 액세서리가 뜨는거야"
    - 오픈 API의 sortCd="A"가 실제 웹사이트 랭킹과 달라 관련성 필터를
    다시 붙였다). conftest의 네트워크 차단(tests/conftest.py) 덕분에
    challenge/judge가 실제로 호출됐다면 이 테스트는 NetworkBlockedError로
    실패한다."""

    async def _no_notes(query, candidates):
        return {}

    monkeypatch.setattr(adk_pipeline.gpt, "candidate_notes", _no_notes)

    items = [
        _item("나이키 에어포스1 케이스", 9900, code="2"),
        _item("나이키 에어포스1 정품", 129000, code="1"),
    ]

    async def _search(query, limit):
        return items

    monkeypatch.setattr(adk_pipeline.elevenst, "search_elevenst_web_ranking", _search)

    result = asyncio.run(adk_pipeline.run("나이키 에어포스1 정품"))

    # 관련성 필터가 "나이키 에어포스1 케이스"(부속품)를 걸러내, API가 1위로
    # 준 것과 무관하게 진짜 본품("나이키 에어포스1 정품")만 남는다.
    assert result.decision.product_name == "나이키 에어포스1 정품"
    assert len(result.proposals) == 1
    assert result.proposals[0].verified is True
    # 2026-08-27, 골든셋 실측 중 발견한 버그 회귀 방지 - Decision.verified가
    # 항상 None으로 나오면 안 된다(challenge를 스킵해도 Proposal 기본값은
    # verified=True이므로 여기서도 True여야 한다).
    assert result.decision.verified is True


def test_run_does_not_call_refine_query_when_skip_gate_already_matched_original(monkeypatch):
    """2026-08-28, 실측 발견 회귀 방지 - "음료수 500ml 병 탄산음료"처럼
    looks_conversational_query가 False라 _skip_refine_if_already_specific이
    LLM 호출 자체를 건너뛰고 원본을 그대로 반환한 경우, _ElevenstSearchNode의
    "refine 결과가 원본과 같으면 재시도" 게이트가 이걸 "정제가 실패했다"로
    오판해 gpt.refine_query를 다시 불렀다. 그 재호출에서 HCX가 이미 구체적인
    검색어를 자기 방식대로 재구성하며 공백을 없애버려("음료수500ml병탄산음료")
    11번가 검색이 0건으로 실패했다. looks_conversational_query가 False인
    질의에서는 gpt.refine_query가 아예 호출되지 않아야 한다."""

    def _boom(query):
        raise AssertionError(
            f"looks_conversational_query가 False인 질의에서 gpt.refine_query가 호출되면 안 된다: {query!r}"
        )

    monkeypatch.setattr(adk_pipeline.gpt, "refine_query", _boom)

    async def _no_notes(query, candidates):
        return {}

    monkeypatch.setattr(adk_pipeline.gpt, "candidate_notes", _no_notes)

    items = [_item("탄산음료 500ml 병", 2000, code="1")]

    async def _search(query, limit):
        return items

    monkeypatch.setattr(adk_pipeline.elevenst, "search_elevenst_web_ranking", _search)

    result = asyncio.run(adk_pipeline.run("음료수 500ml 병 탄산음료"))

    assert result.query == "음료수 500ml 병 탄산음료"
    assert result.decision.product_name == "탄산음료 500ml 병"


def test_run_rescues_relevance_with_deepseek_when_structural_filter_skips(monkeypatch):
    """2026-08-28, 100개 라이브 벤치마크로 실측 발견한 회귀 - 구조적 관련성
    필터(_product_name_matches)가 표기 차이(예: "루미큐브" vs "럼미 큐브")
    때문에 통과율이 임계값 미만으로 나오면, filter_merge는 "이 질의엔 필터가
    안 맞는다"고 보고 필터 자체를 건너뛰고 원본 표본을 무방비로 그대로 썼다
    - challenge/judge가 스킵된 raw 모드에서 검색 API가 준 원본 1위(완전히
    무관한 상품)가 검증 없이 그대로 최종 추천이 됐다. 이제 그 좁은 경우에만
    DeepSeek에게 "실제로 관련 있는 것만 골라달라"고 물어 의미로 재필터링한다."""

    items = [
        _item("완전히 무관한 다른 상품", 5000, code="1"),  # 원본 1위, 검색어와 무관
        _item("럼미 우든 큐브 게임 세트", 30000, code="2"),  # 관련 상품(표기 차이로 구조적 필터를 못 통과)
        _item("또 다른 무관한 상품", 8000, code="3"),
    ]

    async def _search(query, limit):
        return items

    monkeypatch.setattr(adk_pipeline.elevenst, "search_elevenst_web_ranking", _search)

    async def _no_notes(query, candidates):
        return {}

    monkeypatch.setattr(adk_pipeline.gpt, "candidate_notes", _no_notes)

    async def _fake_filter_relevant_indices(query, product_names):
        # DeepSeek이 의미로 재필터링 - 인덱스 1(럼미 우든 큐브 게임 세트)만 관련 있다고 판정.
        return [1]

    monkeypatch.setattr(adk_pipeline.deepseek, "filter_relevant_indices", _fake_filter_relevant_indices)

    result = asyncio.run(adk_pipeline.run("루미큐브 보드게임 가족용"))

    # _product_name_matches만 썼다면 통과율이 낮아 필터를 건너뛰고 원본 1위
    # ("완전히 무관한 다른 상품")가 그대로 최종 추천이 됐을 것이다. DeepSeek
    # 재필터링이 개입해 실제로 관련 있는 상품만 남아야 한다.
    assert result.decision.product_name == "럼미 우든 큐브 게임 세트"


def test_run_rejects_deepseek_relevance_rescue_candidate_with_quantity_conflict(monkeypatch):
    """2026-08-28, 1000개 라이브 벤치마크 재조사로 실측 발견한 회귀 - DeepSeek
    재필터링 프롬프트는 "같은 품목의 다른 묶음"(예: "신라면 5개입 8팩")은
    포함하라고 지시하는데, 이 관대함이 "메로나 20개입" 검색에 "메로나
    30개입"이 통과되는 등 검색어에 명시된 정확한 수량과 다른 후보까지
    새어나가게 했다(judge가 스킵된 raw 모드라 그중 원본 1위가 검증 없이
    그대로 최종 추천이 됨). DeepSeek이 골라준 인덱스에도
    model_or_quantity_conflict(결정적 규칙, LLM 편차 없음)를 한 번 더 적용해
    수량이 명시적으로 다른 후보는 최종적으로 제외한다."""

    items = [
        _item("메로나 메론 30개입 아이스크림", 15000, code="1"),  # 원본 1위, 수량 충돌
        _item("메로나 메론 20개입 아이스크림", 12000, code="2"),  # 검색어와 수량이 일치하는 진짜 정답
    ]

    async def _search(query, limit):
        return items

    monkeypatch.setattr(adk_pipeline.elevenst, "search_elevenst_web_ranking", _search)

    async def _no_notes(query, candidates):
        return {}

    monkeypatch.setattr(adk_pipeline.gpt, "candidate_notes", _no_notes)

    async def _fake_filter_relevant_indices(query, product_names):
        # DeepSeek이 "같은 품목의 다른 묶음"이라며 둘 다 관련 있다고(관대하게) 판정.
        return [0, 1]

    monkeypatch.setattr(adk_pipeline.deepseek, "filter_relevant_indices", _fake_filter_relevant_indices)

    result = asyncio.run(adk_pipeline.run("메로나 20개입"))

    # 수량 충돌 후보(30개입)는 제외되고, 정확한 수량(20개입)만 남아야 한다.
    assert result.decision.product_name == "메로나 메론 20개입 아이스크림"


def test_run_keeps_deepseek_relevance_rescue_when_all_candidates_have_quantity_conflict(monkeypatch):
    """수량 체크 이후 후보가 전부 걸러지면(검색 API 표본에 애초에 정확한
    수량 상품이 하나도 없던 경우) DeepSeek 판정 자체를 무시하지 않고 그대로
    쓴다 - "검증 기회를 한 번 더 준 뒤에도 후보가 아예 없으면 검색 자체를
    막지 않는다"는 기존 폴백 원칙과 동일하게, 수량 불일치 후보라도 관련
    상품 자체가 없는 것보다는 낫다."""

    items = [_item("메로나 메론 30개입 아이스크림", 15000, code="1")]

    async def _search(query, limit):
        return items

    monkeypatch.setattr(adk_pipeline.elevenst, "search_elevenst_web_ranking", _search)

    async def _no_notes(query, candidates):
        return {}

    monkeypatch.setattr(adk_pipeline.gpt, "candidate_notes", _no_notes)

    async def _fake_filter_relevant_indices(query, product_names):
        return [0]

    monkeypatch.setattr(adk_pipeline.deepseek, "filter_relevant_indices", _fake_filter_relevant_indices)

    result = asyncio.run(adk_pipeline.run("메로나 20개입"))

    assert result.decision.product_name == "메로나 메론 30개입 아이스크림"


def test_run_falls_back_to_original_when_deepseek_relevance_rescue_finds_none(monkeypatch):
    """DeepSeek 재필터링이 호출됐지만 실패(None)하거나 "관련 상품이 하나도
    없다"([])는 확정 판정을 내리면, 검색 자체를 막지 않고 기존처럼 원본
    표본을 그대로 쓴다 - 검증 기회를 한 번 더 준 뒤의 최종 안전망이다."""

    items = [_item("아무 상품", 5000, code="1")]

    async def _search(query, limit):
        return items

    monkeypatch.setattr(adk_pipeline.elevenst, "search_elevenst_web_ranking", _search)

    async def _no_notes(query, candidates):
        return {}

    monkeypatch.setattr(adk_pipeline.gpt, "candidate_notes", _no_notes)

    async def _fake_filter_relevant_indices(query, product_names):
        return None  # 호출 실패로 가정

    monkeypatch.setattr(adk_pipeline.deepseek, "filter_relevant_indices", _fake_filter_relevant_indices)

    result = asyncio.run(adk_pipeline.run("전혀 안 맞는 검색어"))

    assert result.decision.product_name == "아무 상품"


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


# ---------------------------------------------------------------------------
# _skip_judge_if_single_candidate - 후보 1개면 LLM 호출 자체를 건너뛴다
# (2026-08-27, 사용자 요청 - "판단 없이 11번가 상위로 뜨는거 그대로
# 가져오면 되잖아". judge는 후보 개수나 challenge 결과와 무관하게 항상
# index=0을 그대로 확정한다).
# ---------------------------------------------------------------------------


def test_skip_judge_always_returns_index_zero_regardless_of_candidate_count():
    ranked = [
        _item("나이키 에어포스1", 129000, code="1"),
        _item("나이키 에어포스1 화이트", 135000, code="2"),
    ]
    ctx = _FakeCallbackContext({"ranked_items": ranked, "proposals": []})

    result = adk_pipeline._skip_judge_always(ctx, llm_request=None)

    assert result is not None
    parsed = adk_pipeline.parse_json_object(adk_pipeline._llm_response_text(result))
    assert parsed["index"] == 0
