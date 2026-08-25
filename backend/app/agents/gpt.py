from openai import AsyncOpenAI

from ..config import settings
from .base import build_candidate_notes_prompt, build_recommend_prompt, build_refine_query_prompt, parse_json_object

# 이 모듈이 담당하는 에이전트 슬롯은 스키마/프론트엔드/테스트 전반에서
# agent="gpt"로 식별된다(파일명·함수명도 그대로) - 하지만 실제로 호출하는
# 모델은 Qwen이다. DashScope가 OpenAI SDK와 호환되는 엔드포인트를 제공해서,
# openai SDK를 base_url만 바꿔 그대로 쓴다(agents/deepseek.py와 동일한
# 패턴). agent="gpt" 식별자 자체를 "qwen"으로 바꾸지 않은 이유 - AgentName
# 리터럴, DB에 저장된 과거 기록, 프론트엔드 타입, 테스트 픽스처 등 수십
# 곳에 걸쳐 있어 그 리네임 자체가 훨씬 큰 변경이 된다. 사용자에게 보이는
# 이름만 frontend/src/app/components/SearchResults.tsx의 AGENT_LABEL에서
# "Qwen"으로 바꿔뒀다.


# 2026-08-24 실측 - 호출마다 AsyncOpenAI를 새로 만들면 매번 TCP/TLS
# 핸드셰이크를 새로 맺어 호출당 ~0.7초가 그냥 날아간다(연결 재사용 시
# 2.33초 -> 1.59초로 단축 확인, recommend_best/candidate_notes가 매 검색마다
# 이 클라이언트를 새로 만들던 게 응답 지연의 숨은 원인이었다). 모듈
# 레벨에 캐싱해 한 번만 만들고 재사용한다.
_client_instance: AsyncOpenAI | None = None


def _client() -> AsyncOpenAI:
    # max_retries=0 - 사용자 요청(2026-08-15: "너무
    # 느려 더 빠르게"). 실패해도 호출부가 이미 폴백을 갖고 있어 SDK 재시도로
    # 얻는 이득보다 지연 비용이 크다.
    global _client_instance
    if _client_instance is None:
        _client_instance = AsyncOpenAI(api_key=settings.qwen_api_key, base_url=settings.qwen_api_base, max_retries=0)
    return _client_instance


# qwen3.7-plus는 DashScope 쪽에서 기본적으로 "thinking mode"(내부 추론 과정을
# 다 생성한 뒤에야 응답을 반환)가 켜져 있다(2026-08-20 실측 - enable_thinking을
# 안 주면 단순 질문 하나에도 20~95초가 걸린다, extra_body={"enable_thinking":
# False}를 주면 2초로 줄어든다). 이 프로젝트가 쓰는 프롬프트는 전부 JSON 한
# 덩어리만 필요해서 추론 과정 자체가 필요 없다 - 이 모듈의 모든 호출에 끈다.
_DISABLE_THINKING = {"enable_thinking": False}


async def refine_query(query: str) -> str | None:
    """app.intent.looks_conversational_query()에 걸린 질의("저렴한 아기 간식을
    사고 싶어" 등)에서 인사말·구매 의도 표현을 걷어내고 실제 상품명(또는
    상품 종류)만 남긴다(2026-08-24 사용자 리포트 - 정제 없이 그대로 11번가
    keyword로 넘어가 검색이 실패했다). 실패(키 없음·API 오류·빈 결과)하면
    None - 호출부가 원래 질의 그대로 검색을 진행한다(정제 실패가 검색
    자체를 막으면 안 된다, app.embeddings.embed와 같은 폴백 원칙)."""
    stripped = query.strip()
    if not stripped:
        return None
    try:
        client = _client()
        response = await client.chat.completions.create(
            model=settings.qwen_model,
            messages=[{"role": "user", "content": build_refine_query_prompt(stripped)}],
            response_format={"type": "json_object"},
            extra_body=_DISABLE_THINKING,
        )
        data = parse_json_object(response.choices[0].message.content or "")
        refined = str(data.get("query") or "").strip()
        return refined or None
    except Exception:
        return None


async def recommend_best(query: str, candidates: list[dict]) -> tuple[int, str] | None:
    """11번가 검증 후보(app.debate.run_elevenst_only_debate) 중 가장 추천할
    만한 것을 LLM이 고른다 - 가격만이 아니라 리뷰 수/구매만족도까지 본다.
    실패(키 없음·API 오류·범위 밖 index)하면 None - 호출부가 최저가 규칙
    기반으로 폴백한다.

    2026-08-24: 후보별 개별 이유(candidate_notes)를 처음엔 이 호출 응답에
    같이 얹었는데(출력 토큰이 늘어 평균 3.2초 -> 10.3초, 최대 20초까지 튐 -
    직접 실측), index 선택과 개별 이유 생성은 서로 의존하지 않는 별개
    작업이라 별도 함수(candidate_notes)로 분리했다 - app.debate가 이 함수와
    asyncio.gather로 동시에 호출해, 전체 대기시간이 두 호출의 합이 아니라
    더 느린 쪽 하나로 수렴하게 한다."""
    if not candidates:
        return None
    try:
        client = _client()
        response = await client.chat.completions.create(
            model=settings.qwen_model,
            messages=[{"role": "user", "content": build_recommend_prompt(query, candidates)}],
            response_format={"type": "json_object"},
            extra_body=_DISABLE_THINKING,
        )
        data = parse_json_object(response.choices[0].message.content or "")
        index = int(data.get("index"))
        if not (0 <= index < len(candidates)):
            return None
        return index, str(data.get("reasoning") or "").strip()
    except Exception:
        return None


# "다른 후보" 카드가 최대 몇 개까지 노출되는지(frontend/.../SearchResults.tsx의
# MAX_OTHER_PROPOSALS)와 맞춰, 그만큼의 후보에 대해서만 개별 이유를 요청한다 -
# 후보가 90개까지 갈 수 있는 드릴다운 검색에서 전부에 대해 이유를 받으면
# 토큰 낭비다.
_MAX_CANDIDATE_NOTES = 5


async def candidate_notes(query: str, candidates: list[dict]) -> dict[int, str]:
    """recommend_best()의 index 선택과 별개로, 상위 후보들 각각에 대한
    1문장 이유를 받아온다(2026-08-24 사용자 요청 - "후보 추천해주는 애들도
    추천하는 이유가 있으면 좋겠다"). recommend_best와 asyncio.gather로 동시에
    호출되는 걸 전제로 한다 - 이 함수 혼자 실패해도(키 없음·API 오류·JSON
    파싱 실패) 빈 dict만 돌려주고, 호출부가 일반 문구로 안전하게 대체한다."""
    if not candidates:
        return {}
    note_indices = list(range(min(_MAX_CANDIDATE_NOTES, len(candidates))))
    try:
        client = _client()
        response = await client.chat.completions.create(
            model=settings.qwen_model,
            messages=[{"role": "user", "content": build_candidate_notes_prompt(query, candidates, note_indices)}],
            response_format={"type": "json_object"},
            extra_body=_DISABLE_THINKING,
        )
        data = parse_json_object(response.choices[0].message.content or "")
        notes: dict[int, str] = {}
        for key, value in (data.get("notes") or {}).items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(candidates) and value:
                notes[index] = str(value).strip()
        return notes
    except Exception:
        return {}
