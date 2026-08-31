"""벤치마크 비용 지표용 요청-스코프 토큰 사용량 집계(2026-08-31).

ADK LlmAgent 노드는 Event.usage_metadata로 토큰 사용량이 자동으로 잡히지만,
이 코드베이스의 여러 단계(apply_challenge의 candidate_notes 등)는 ADK 밖에서
AsyncOpenAI/OpenAI 호환 클라이언트를 직접 호출하는 순수 함수라 그 혜택을 못
받는다. 이 모듈은 그런 직접 호출 지점이 response.usage를 여기 등록하도록
공통 지점을 하나로 모은다 - main.py가 요청 끝에 이 값을 읽어 X-Usage 헤더로
노출한다."""

from __future__ import annotations

from contextvars import ContextVar

usage_by_node: ContextVar[dict[str, dict[str, int]]] = ContextVar("usage_by_node", default={})


def reset() -> None:
    usage_by_node.set({})


def record(node: str, prompt_tokens: int | None, completion_tokens: int | None) -> None:
    current = usage_by_node.get()
    bucket = dict(current.get(node) or {"prompt_tokens": 0, "completion_tokens": 0})
    bucket["prompt_tokens"] += prompt_tokens or 0
    bucket["completion_tokens"] += completion_tokens or 0
    usage_by_node.set({**current, node: bucket})


def record_openai_response(node: str, response) -> None:
    """OpenAI SDK(및 호환 엔드포인트) ChatCompletion 응답의 usage를 그대로 기록한다."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    record(node, getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None))


def merge(other: dict[str, dict[str, int]]) -> None:
    """ADK Event.usage_metadata 집계 결과(dict) 등 외부에서 만든 노드별 합계를
    현재 contextvar 값에 병합한다."""
    current = usage_by_node.get()
    merged = {k: dict(v) for k, v in current.items()}
    for node, bucket in other.items():
        target = merged.setdefault(node, {"prompt_tokens": 0, "completion_tokens": 0})
        target["prompt_tokens"] += bucket.get("prompt_tokens") or 0
        target["completion_tokens"] += bucket.get("completion_tokens") or 0
    usage_by_node.set(merged)
