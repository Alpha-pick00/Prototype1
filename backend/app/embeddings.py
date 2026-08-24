"""Qwen(DashScope) text-embedding-v3 기반 임베딩 유틸 - run_elevenst_only_debate의
"관련 상품" 랭킹과 향후 시맨틱 캐시가 공유해서 쓴다. 실측(2026-08-20)으로
text-embedding-v3(1024차원)만 이 계정 플랜에서 접근 가능함을 확인했다 -
text-embedding-v1/v2는 각각 404/AccessDenied로 막혀 있다."""

from __future__ import annotations

import math

from openai import AsyncOpenAI

from .config import settings

EMBEDDING_MODEL = "text-embedding-v3"

# 2026-08-24 실측 - 호출마다 AsyncOpenAI를 새로 만들면 매번 TCP/TLS
# 핸드셰이크를 새로 맺어 호출당 ~0.7초가 그냥 날아간다(연결 재사용 시
# 2.33초 -> 1.59초로 단축 확인). 모듈 레벨에 캐싱해 한 번만 만들고
# 재사용한다 - httpx의 커넥션 풀이 keep-alive를 알아서 관리해준다.
_client_instance: AsyncOpenAI | None = None


def _client() -> AsyncOpenAI:
    global _client_instance
    if _client_instance is None:
        _client_instance = AsyncOpenAI(api_key=settings.qwen_api_key, base_url=settings.qwen_api_base, max_retries=0)
    return _client_instance


# 2026-08-24 실측(BadRequestError: "batch size is invalid, it should not be
# larger than 10") - DashScope text-embedding-v3의 하드 제한. 검색 결과
# 기본 limit(10)에서 전부 관련성 필터를 통과하면 [query, *10개 상품명] = 11개로
# 이 제한을 조용히 넘겨 임베딩 전체가 실패하고(return None) 랭킹이 11번가
# 원래 순서로 조용히 폴백되는 문제가 있었다 - 흔한 케이스(꽉 찬 검색 결과)를
# 매번 조용히 망가뜨리고 있었다는 뜻이라 조용히 넘길 수 없는 버그.
_MAX_EMBED_BATCH = 10


async def embed(texts: list[str]) -> list[list[float]] | None:
    """실패(키 없음·API 오류)하면 None을 돌려준다 - 호출부는 임베딩 없이
    원래 순서를 그대로 쓰는 폴백을 갖는다. 10개 넘는 texts는 여러 번 나눠
    호출해 이어붙인다(위 _MAX_EMBED_BATCH 설명 참고) - 호출부가 이 제한을
    직접 신경 쓸 필요 없도록 여기서 흡수한다."""
    if not texts or not settings.qwen_api_key:
        return None
    try:
        client = _client()
        vectors: list[list[float]] = []
        for i in range(0, len(texts), _MAX_EMBED_BATCH):
            chunk = texts[i : i + _MAX_EMBED_BATCH]
            response = await client.embeddings.create(model=EMBEDDING_MODEL, input=chunk)
            vectors.extend(d.embedding for d in response.data)
        return vectors
    except Exception:
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
