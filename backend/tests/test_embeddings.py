"""app.embeddings.embed()의 배치 분할 테스트. 실측(2026-08-24,
BadRequestError: "batch size is invalid, it should not be larger than 10") -
DashScope text-embedding-v3는 한 호출에 10개 초과 texts를 넘기면 통째로
실패한다. embed()가 내부적으로 10개씩 나눠 호출하고 이어붙이는지 확인한다.
전부 monkeypatch - 네트워크 요청 없음."""

from __future__ import annotations

import asyncio

from app import embeddings


class _FakeResponseItem:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _FakeResponse:
    def __init__(self, data: list[_FakeResponseItem]) -> None:
        self.data = data


class _FakeEmbeddingsAPI:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def create(self, model: str, input: list[str]) -> _FakeResponse:
        self.calls.append(list(input))
        assert len(input) <= 10, "한 번에 10개 넘게 보내면 안 된다(DashScope 하드 제한)"
        return _FakeResponse([_FakeResponseItem([float(len(text))]) for text in input])


class _FakeClient:
    def __init__(self) -> None:
        self.embeddings = _FakeEmbeddingsAPI()


def test_embed_splits_more_than_ten_texts_into_multiple_batches(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(embeddings, "_client", lambda: fake_client)
    monkeypatch.setattr("app.config.settings.qwen_api_key", "fake-key")

    texts = [f"상품{i}" for i in range(23)]  # 10개씩 3번(10, 10, 3)

    result = asyncio.run(embeddings.embed(texts))

    assert len(fake_client.embeddings.calls) == 3
    assert [len(call) for call in fake_client.embeddings.calls] == [10, 10, 3]
    assert result == [[float(len(text))] for text in texts]


def test_embed_single_batch_still_works(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(embeddings, "_client", lambda: fake_client)
    monkeypatch.setattr("app.config.settings.qwen_api_key", "fake-key")

    result = asyncio.run(embeddings.embed(["망고주스"]))

    assert len(fake_client.embeddings.calls) == 1
    assert result == [[float(len("망고주스"))]]


def test_embed_returns_none_when_api_key_missing(monkeypatch):
    monkeypatch.setattr("app.config.settings.qwen_api_key", None)

    assert asyncio.run(embeddings.embed(["망고주스"])) is None
