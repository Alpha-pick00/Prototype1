"""run_elevenst_only_debate의 추천 Agent(임베딩 관련도 랭킹 + LLM 최종 선택)
테스트. 네트워크 요청 금지 - 전부 monkeypatch."""

from __future__ import annotations

import asyncio

from app import debate
from fetchers.elevenst import ElevenstSearchItem


def _item(
    name: str,
    price: int,
    code: str = "1",
    image_url: str | None = None,
    review_count: int | None = None,
    buy_satisfy: int | None = None,
) -> ElevenstSearchItem:
    return ElevenstSearchItem(
        product_code=code,
        product_name=name,
        price_krw=price,
        seller="판매자",
        url=f"https://www.11st.co.kr/products/{code}",
        review_count=review_count,
        buy_satisfy=buy_satisfy,
        image_url=image_url,
    )


async def _collect_stream(query: str, **kwargs) -> list[dict]:
    return [event async for event in debate.run_elevenst_only_debate_stream(query, **kwargs)]


def test_search_candidates_searches_directly_when_no_base_query(monkeypatch):
    seen = {}

    async def _fake_search(query, limit=10):
        seen["query"] = query
        seen["limit"] = limit
        return [_item("아무 상품", 1000, "1")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)

    items = asyncio.run(debate._search_candidates("초코파이", None))

    assert seen == {"query": "초코파이", "limit": debate.price_table_module.SINGLE_QUERY_SEARCH_LIMIT}
    assert [it["product_code"] for it in items] == ["1"]


def test_search_candidates_reuses_base_query_search_and_filters_structurally_when_drilled_down(monkeypatch):
    """HITL 드릴다운 후속 턴(query != base_query)이면 재구성된 전체 문자열로
    다시 검색하지 않고, base_query로 넓게 검색한 뒤 사용자가 덧붙인 답을
    로컬 필터링(_filter_items_by_extra_terms)으로 구조적으로 좁혀야 한다."""
    seen = {}

    async def _fake_search(query, limit=10):
        seen["query"] = query
        seen["limit"] = limit
        return [
            _item("초코파이 오리온 바나나 468g", 3000, "1"),
            _item("초코파이 오리온 정 39g", 1500, "2"),
            _item("초코파이 오리온 말차 468g", 3200, "3"),
            _item("초코파이 롯데 딸기 300g", 2000, "4"),
        ]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)

    items = asyncio.run(debate._search_candidates("초코파이 오리온", "초코파이"))

    assert seen == {"query": "초코파이", "limit": debate.price_table_module.CLARIFY_SEARCH_LIMIT}
    assert [it["product_code"] for it in items] == ["1", "2", "3"]


def test_rank_by_relevance_orders_by_cosine_similarity_when_embeddings_available(monkeypatch):
    items = [_item("무관한 상품", 1000, "1"), _item("정확히 찾는 상품", 2000, "2")]

    async def _fake_embed(texts):
        # texts[0]는 query, 나머지는 후보 이름 순서대로 - "정확히 찾는 상품"만
        # query와 코사인 유사도 1.0이 되도록 벡터를 짠다.
        return [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    ranked = asyncio.run(debate._rank_by_relevance("정확히 찾는 상품", items))

    assert [it["product_code"] for it in ranked] == ["2", "1"]


def test_rank_by_relevance_falls_back_to_original_order_when_embedding_fails(monkeypatch):
    items = [_item("상품A", 1000, "1"), _item("상품B", 2000, "2")]

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    ranked = asyncio.run(debate._rank_by_relevance("아무거나", items))

    assert [it["product_code"] for it in ranked] == ["1", "2"]


async def _no_notes(query, candidates):
    return {}


def test_run_elevenst_only_debate_uses_recommend_agent_pick_over_cheapest(monkeypatch):
    """추천 Agent가 최저가가 아닌 후보를 골라도(리뷰/구매만족도 등을 근거로)
    그 선택을 최종 추천으로 써야 한다 - 무조건 최저가를 강제하지 않는다."""

    async def _fake_search(query, limit=10):
        return [_item("찾는 상품 A", 1000, "1"), _item("찾는 상품 B", 2000, "2")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None  # 순서 그대로 유지

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_recommend(query, candidates):
        return 1, "리뷰가 훨씬 많아서 더 신뢰할 만함"

    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _no_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("찾는 상품"))

    assert result.decision.product_name == "찾는 상품 B"
    assert result.decision.price == "2,000원"
    assert "추천 Agent" in result.decision.reasoning
    assert len(result.proposals) == 2


def test_run_elevenst_only_debate_drops_reasoning_that_leaks_internal_index(monkeypatch):
    """추천 Agent 프롬프트가 후보를 "[0] 상품명 / ..."처럼 번호 매겨 보여주는데,
    LLM이 그 내부 순번을 그대로 reasoning에 써버리면(실측: "index 14", "(0, 1)")
    사용자는 본 적 없는 번호라 무슨 뜻인지 모른다 - 그런 reasoning은 버리고
    일반 문구로 대체해야 한다."""

    async def _fake_search(query, limit=10):
        return [_item("찾는 상품 A", 1000, "1"), _item("찾는 상품 B", 2000, "2")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_recommend(query, candidates):
        return 1, "index 1의 상품이 다른 후보(0)보다 리뷰가 많아서 선택함"

    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _no_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("찾는 상품"))

    assert "index" not in result.decision.reasoning.lower()
    assert result.decision.reasoning == "11번가 실측 검증 후보 중 추천 Agent(HCX)가 선택"


def test_run_elevenst_only_debate_uses_candidate_notes_for_proposal_reasoning(monkeypatch):
    """"다른 후보"로 노출되는 각 Proposal이 candidate_notes가 준 개별
    이유를 쓰는지 확인한다 - 전부 같은 문구만 달려 있으면 클릭해도 설명이
    없는 것처럼 느껴진다는 사용자 피드백(2026-08-24) 회귀 테스트.
    recommend_best와 candidate_notes는 asyncio.gather로 동시에 호출되므로
    서로 독립적인 별도 함수로 monkeypatch한다."""

    async def _fake_search(query, limit=10):
        return [_item("찾는 상품 A", 1000, "1"), _item("찾는 상품 B", 2000, "2")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_recommend(query, candidates):
        return 0, "가장 저렴함"

    async def _fake_notes(query, candidates):
        return {0: "가장 저렴하고 리뷰도 많음", 1: "용량이 더 커서 대용량이 필요하면 적합"}

    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _fake_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("찾는 상품"))

    by_name = {p.product_name: p.reasoning for p in result.proposals}
    assert by_name["찾는 상품 A"] == "가장 저렴하고 리뷰도 많음"
    assert by_name["찾는 상품 B"] == "용량이 더 커서 대용량이 필요하면 적합"


def test_run_elevenst_only_debate_falls_back_to_generic_note_when_note_leaks_index(monkeypatch):
    async def _fake_search(query, limit=10):
        return [_item("찾는 상품 A", 1000, "1"), _item("찾는 상품 B", 2000, "2")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_recommend(query, candidates):
        return 0, "가장 저렴함"

    async def _fake_notes(query, candidates):
        return {1: "index 0보다 비쌈"}

    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _fake_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("찾는 상품"))

    by_name = {p.product_name: p.reasoning for p in result.proposals}
    assert by_name["찾는 상품 B"] == "11번가 오픈 API 검증 결과 (관련도순 - 함께 볼만한 상품)"


def test_run_elevenst_only_debate_propagates_image_url_to_decision_and_proposals(monkeypatch):
    """카드 UI가 쓸 image_url이 ElevenstSearchItem -> Decision/Proposal까지
    끊기지 않고 전달되는지 확인한다."""

    async def _fake_search(query, limit=10):
        return [
            _item("찾는 상품 A", 1000, "1", image_url="https://cdn.011st.com/a.webp"),
            _item("찾는 상품 B", 2000, "2", image_url="https://cdn.011st.com/b.webp"),
        ]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_recommend(query, candidates):
        return 0, "가장 저렴함"

    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _no_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("찾는 상품"))

    assert result.decision.image_url == "https://cdn.011st.com/a.webp"
    by_code = {p.url: p.image_url for p in result.proposals}
    assert by_code["https://www.11st.co.kr/products/1"] == "https://cdn.011st.com/a.webp"
    assert by_code["https://www.11st.co.kr/products/2"] == "https://cdn.011st.com/b.webp"


def test_run_elevenst_only_debate_propagates_review_count_and_buy_satisfy_to_proposals(monkeypatch):
    """프론트가 "만족도 최고" 배지를 계산하려면(2026-08-24, 사용자 요청)
    review_count/buy_satisfy가 Proposal까지 전달돼야 한다."""

    async def _fake_search(query, limit=10):
        return [
            _item("찾는 상품 A", 1000, "1", review_count=5, buy_satisfy=90),
            _item("찾는 상품 B", 2000, "2", review_count=None, buy_satisfy=None),
        ]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_recommend(query, candidates):
        return 0, "가장 저렴함"

    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _no_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("찾는 상품"))

    by_name = {p.product_name: (p.review_count, p.buy_satisfy) for p in result.proposals}
    assert by_name["찾는 상품 A"] == (5, 90)
    assert by_name["찾는 상품 B"] == (None, None)


def test_run_elevenst_only_debate_falls_back_to_cheapest_when_recommend_agent_fails(monkeypatch):
    async def _fake_search(query, limit=10):
        return [_item("찾는 상품 A", 2000, "1"), _item("찾는 상품 B", 1000, "2")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_recommend(query, candidates):
        return None

    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _no_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("찾는 상품"))

    assert result.decision.product_name == "찾는 상품 B"
    assert result.decision.price == "1,000원"
    assert "최저가" in result.decision.reasoning


def test_recommend_best_returns_none_for_out_of_range_index(monkeypatch):
    from app.agents import gpt

    class _FakeMessage:
        content = '{"index": 5, "reasoning": "아무거나"}'

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResponse:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(gpt, "_client", lambda: _FakeClient())

    result = asyncio.run(gpt.recommend_best("질의", [{"product_name": "상품", "price_krw": 1000, "seller": "s"}]))

    assert result is None


def test_recommend_best_returns_index_and_reasoning_on_success(monkeypatch):
    from app.agents import gpt

    class _FakeMessage:
        content = '{"index": 0, "reasoning": "리뷰가 많음"}'

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResponse:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(gpt, "_client", lambda: _FakeClient())

    result = asyncio.run(gpt.recommend_best("질의", [{"product_name": "상품", "price_krw": 1000, "seller": "s"}]))

    assert result == (0, "리뷰가 많음")


def test_candidate_notes_parses_string_keys_into_int_keys(monkeypatch):
    """LLM은 JSON으로 응답하므로 notes의 키는 항상 문자열("0", "1")이다 -
    호출부(app.debate)가 ranked 리스트를 정수 인덱스로 찾으니 int로 변환해야
    한다."""
    from app.agents import gpt

    class _FakeMessage:
        content = '{"notes": {"0": "리뷰 많고 저렴함", "1": "가성비 좋음"}}'

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResponse:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(gpt, "_client", lambda: _FakeClient())

    candidates = [
        {"product_name": "상품A", "price_krw": 1000, "seller": "s"},
        {"product_name": "상품B", "price_krw": 2000, "seller": "s"},
    ]
    result = asyncio.run(gpt.candidate_notes("질의", candidates))

    assert result == {0: "리뷰 많고 저렴함", 1: "가성비 좋음"}


def test_candidate_notes_drops_out_of_range_indices(monkeypatch):
    from app.agents import gpt

    class _FakeMessage:
        content = '{"notes": {"0": "괜찮음", "9": "범위 밖"}}'

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResponse:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(gpt, "_client", lambda: _FakeClient())

    result = asyncio.run(gpt.candidate_notes("질의", [{"product_name": "상품", "price_krw": 1000, "seller": "s"}]))

    assert result == {0: "괜찮음"}


def test_candidate_notes_returns_empty_dict_on_failure(monkeypatch):
    from app.agents import gpt

    class _FakeCompletions:
        async def create(self, **kwargs):
            raise RuntimeError("API 오류")

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(gpt, "_client", lambda: _FakeClient())

    result = asyncio.run(gpt.candidate_notes("질의", [{"product_name": "상품", "price_krw": 1000, "seller": "s"}]))

    assert result == {}


def test_candidate_notes_returns_empty_dict_for_no_candidates():
    from app.agents import gpt

    result = asyncio.run(gpt.candidate_notes("질의", []))

    assert result == {}


def test_stream_yields_final_before_candidate_notes_resolves(monkeypatch):
    """체감 속도 개선(2026-08-24, "메인 추천이 끝나는 대로 먼저 보여주고 다른
    후보 이유는 나중에 채워 넣기") 회귀 테스트 - candidate_notes를 일부러
    안 끝나게 묶어둔 채로 final이 이미 도착해야 하고(일반 문구로), 그 뒤에
    notes 이벤트로 실제 이유가 와야 한다."""

    async def _fake_search(query, limit=10):
        return [_item("찾는 상품 A", 1000, "1"), _item("찾는 상품 B", 2000, "2")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_recommend(query, candidates):
        return 0, "가장 저렴함"

    notes_release = asyncio.Event()

    async def _fake_notes(query, candidates):
        await notes_release.wait()
        return {0: "진짜 이유 A", 1: "진짜 이유 B"}

    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _fake_notes)

    async def _run():
        gen = debate.run_elevenst_only_debate_stream("찾는 상품")
        status_event = await gen.__anext__()
        final_event = await gen.__anext__()
        # final이 이 시점에 이미 왔다는 것 자체가, candidate_notes를 기다리지
        # 않았다는 증거다(release 신호를 아직 안 보냈는데도 final을 받았다).
        assert not notes_release.is_set()
        notes_release.set()
        notes_event = await gen.__anext__()
        return status_event, final_event, notes_event

    status_event, final_event, notes_event = asyncio.run(_run())

    assert status_event == {"type": "status", "stage": "searching"}

    assert final_event["type"] == "final"
    final_proposals = final_event["result"]["proposals"]
    assert all(
        p["reasoning"] == "11번가 오픈 API 검증 결과 (관련도순 - 함께 볼만한 상품)" for p in final_proposals
    )

    assert notes_event["type"] == "notes"
    by_name = {p["product_name"]: p["reasoning"] for p in notes_event["proposals"]}
    assert by_name["찾는 상품 A"] == "진짜 이유 A"
    assert by_name["찾는 상품 B"] == "진짜 이유 B"


def test_stream_skips_notes_event_when_candidate_notes_come_back_empty(monkeypatch):
    """candidate_notes가 실패하거나(빈 dict) 아무 이유도 못 주면, 이미 final에
    일반 문구로 채워둔 proposals를 굳이 다시 갈아끼울 필요가 없다 - notes
    이벤트 자체를 생략해 프론트에 쓸모없는 패치를 안 보낸다."""

    async def _fake_search(query, limit=10):
        return [_item("찾는 상품 A", 1000, "1")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_recommend(query, candidates):
        return 0, "가장 저렴함"

    async def _fake_notes(query, candidates):
        return {}

    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _fake_notes)

    events = asyncio.run(_collect_stream("찾는 상품"))

    assert [e["type"] for e in events] == ["status", "final"]


def test_refine_query_returns_cleaned_query_on_success(monkeypatch):
    """2026-08-24 사용자 리포트 - "저렴한 아기 간식을 사고 싶어"처럼 대화체
    질의를 그대로 11번가 keyword로 넘기면 검색이 실패한다."""
    from app.agents import gpt

    class _FakeMessage:
        content = '{"query": "저렴한 아기 간식"}'

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResponse:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(gpt, "_client", lambda: _FakeClient())

    result = asyncio.run(gpt.refine_query("저렴한 아기 간식을 사고 싶어"))

    assert result == "저렴한 아기 간식"


def test_refine_query_returns_none_on_failure(monkeypatch):
    from app.agents import gpt

    class _FakeCompletions:
        async def create(self, **kwargs):
            raise RuntimeError("API 오류")

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(gpt, "_client", lambda: _FakeClient())

    assert asyncio.run(gpt.refine_query("저렴한 아기 간식을 사고 싶어")) is None


def test_run_elevenst_only_debate_refines_conversational_query_before_search(monkeypatch):
    """대화체 질의는 정제된 검색어로 11번가를 검색해야 하고, 최종 응답의
    query 필드도 정제된 검색어를 반영해야 한다(추천 Agent 프롬프트/에러
    메시지와 일관성 유지)."""
    seen_search_query = {}

    async def _fake_search(query, limit=10):
        seen_search_query["query"] = query
        return [_item("아기 간식 세트", 5000, "1")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_refine(query):
        return "저렴한 아기 간식"

    seen_recommend_query = {}

    async def _fake_recommend(query, candidates):
        seen_recommend_query["query"] = query
        return 0, "가성비 좋음"

    monkeypatch.setattr(debate.gpt, "refine_query", _fake_refine)
    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _no_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("저렴한 아기 간식을 사고 싶어"))

    assert seen_search_query["query"] == "저렴한 아기 간식"
    assert seen_recommend_query["query"] == "저렴한 아기 간식"
    assert result.query == "저렴한 아기 간식"


def test_run_elevenst_only_debate_skips_refine_for_clean_query(monkeypatch):
    """이미 짧고 깨끗한 검색어는 정제(LLM 호출) 자체를 건너뛰어야 한다 -
    불필요한 지연/비용을 늘리지 않는다."""

    async def _fake_search(query, limit=10):
        return [_item("찾는 상품 A", 1000, "1")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _boom_refine(query):
        raise AssertionError("깨끗한 검색어인데 refine_query가 호출됐다")

    async def _fake_recommend(query, candidates):
        return 0, "가장 저렴함"

    monkeypatch.setattr(debate.gpt, "refine_query", _boom_refine)
    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _no_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("찾는 상품"))

    assert result.query == "찾는 상품"


def test_run_elevenst_only_debate_falls_back_to_original_query_when_refine_fails(monkeypatch):
    async def _fake_search(query, limit=10):
        return [_item("찾는 상품 A", 1000, "1")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_refine(query):
        return None  # 실패(키 없음·API 오류)

    async def _fake_recommend(query, candidates):
        return 0, "가장 저렴함"

    monkeypatch.setattr(debate.gpt, "refine_query", _fake_refine)
    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _no_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("아기 간식을 사고 싶어"))

    # 정제 실패해도 원래 질의 그대로 검색을 계속 진행해야 한다.
    assert result.query == "아기 간식을 사고 싶어"


def test_run_elevenst_only_debate_uses_semantic_fallback_when_rapidfuzz_rejects_everything(monkeypatch):
    """실측(2026-08-24, "망고주스를 사고 싶어" 검색 실패) - rapidfuzz가 표기
    차이(붙여쓰기 vs 쪼개서 다른 순서)로 진짜 매치를 전부 거부해도,
    semantic_relevance_fallback이 임베딩 유사도로 구제하면 검색이 성공해야
    한다."""

    async def _fake_search(query, limit=10):
        return [_item("카프리썬 오렌지망고 200ml x 40입 주스", 15000, "1")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    # rapidfuzz 기반 1차 필터는 항상 거부(실측처럼 표기 차이로 전부 탈락하는 상황을 재현).
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: False)

    async def _fake_embed(texts):
        return [[1.0, 0.0] if t == "망고주스" else [0.9, 0.1] for t in texts]

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_recommend(query, candidates):
        return 0, "가장 관련성 높음"

    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _no_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("망고주스"))

    assert result.decision.product_name == "카프리썬 오렌지망고 200ml x 40입 주스"


def test_run_elevenst_only_debate_skips_semantic_fallback_for_facet_drilldown(monkeypatch):
    """facet_answers가 있는 드릴다운 경로는 이미 _filter_items_by_facet_answers로
    걸러진 결과라 semantic_relevance_fallback을 또 태우면 안 된다(불필요한
    임베딩 호출) - 0건이면 그냥 대안 표기 재검색으로 넘어가야 한다."""

    async def _fake_search(query, limit=10):
        return []

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)

    async def _boom_semantic_fallback(query, items):
        raise AssertionError("facet_answers 드릴다운인데 semantic_relevance_fallback이 호출됐다")

    monkeypatch.setattr(debate.price_table_module, "semantic_relevance_fallback", _boom_semantic_fallback)

    async def _fake_variants(query):
        return []

    monkeypatch.setattr(debate.hcx, "generate_query_variants", _fake_variants)

    try:
        asyncio.run(
            debate.run_elevenst_only_debate(
                "찾는 상품 브랜드A", base_query="찾는 상품", facet_answers={"브랜드": ["브랜드A"]}
            )
        )
        raise AssertionError("RuntimeError가 발생해야 한다")
    except RuntimeError as exc:
        assert "관련성 있는 상품을 찾지 못했습니다" in str(exc)


def test_run_elevenst_only_debate_filters_by_price_condition_before_recommending(monkeypatch):
    """"망고주스 2만원대로 사고 싶어" - 가격 조건에 맞는 후보가 있으면 그
    범위 안의 후보만 추천 Agent에게 넘겨야 한다(범위 밖 후보가 섞여서
    엉뚱하게 뽑히면 안 됨)."""

    async def _fake_search(query, limit=10):
        assert query == "망고주스"
        return [
            _item("망고주스 A", 15000, "1"),
            _item("망고주스 B", 25000, "2"),
            _item("망고주스 C", 45000, "3"),
        ]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_refine(query):
        return "망고주스"

    monkeypatch.setattr(debate.gpt, "refine_query", _fake_refine)

    seen_candidates = {}

    async def _fake_recommend(query, candidates):
        seen_candidates["candidates"] = candidates
        return 0, "가격대에 맞음"

    monkeypatch.setattr(debate.gpt, "recommend_best", _fake_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _no_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("망고주스 2만원대로 사고 싶어"))

    # 2만원대(20000~29999) 안에 드는 건 "망고주스 B"(25000원) 하나뿐이다.
    assert [c["product_name"] for c in seen_candidates["candidates"]] == ["망고주스 B"]
    assert result.decision.product_name == "망고주스 B"


def test_run_elevenst_only_debate_falls_back_to_closest_price_when_none_in_range(monkeypatch):
    """가격 조건에 맞는 후보가 하나도 없으면 추천 Agent를 부르지 않고,
    가격이 가장 근접한 상품을 규칙 기반으로 안내해야 한다."""

    async def _fake_search(query, limit=10):
        return [
            _item("망고주스 A", 39000, "1"),
            _item("망고주스 B", 45000, "2"),
        ]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_refine(query):
        return "망고주스"

    monkeypatch.setattr(debate.gpt, "refine_query", _fake_refine)

    async def _boom_recommend(query, candidates):
        raise AssertionError("가격 조건 미충족 시 recommend_best가 호출되면 안 된다")

    monkeypatch.setattr(debate.gpt, "recommend_best", _boom_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _no_notes)

    result = asyncio.run(debate.run_elevenst_only_debate("망고주스 2만원대로 사고 싶어"))

    # 39000원이 45000원보다 2만원대(상한 29999)에 더 가깝다.
    assert result.decision.product_name == "망고주스 A"
    assert "20,000원~29,999원" in result.decision.reasoning
    assert "찾지 못해" in result.decision.reasoning


def test_run_elevenst_only_debate_stream_falls_back_to_closest_price_when_none_in_range(monkeypatch):
    async def _fake_search(query, limit=10):
        return [_item("망고주스 A", 39000, "1"), _item("망고주스 B", 45000, "2")]

    monkeypatch.setattr("fetchers.elevenst.search_elevenst", _fake_search)
    monkeypatch.setattr(debate.price_table_module, "_product_name_matches", lambda a, b: True)

    async def _fake_embed(texts):
        return None

    monkeypatch.setattr(debate.embeddings, "embed", _fake_embed)

    async def _fake_refine(query):
        return "망고주스"

    monkeypatch.setattr(debate.gpt, "refine_query", _fake_refine)

    async def _boom_recommend(query, candidates):
        raise AssertionError("가격 조건 미충족 시 recommend_best가 호출되면 안 된다")

    monkeypatch.setattr(debate.gpt, "recommend_best", _boom_recommend)
    monkeypatch.setattr(debate.gpt, "candidate_notes", _no_notes)

    events = asyncio.run(_collect_stream("망고주스 2만원대로 사고 싶어"))

    final = next(e for e in events if e["type"] == "final")
    assert final["result"]["decision"]["product_name"] == "망고주스 A"
    assert "찾지 못해" in final["result"]["decision"]["reasoning"]
