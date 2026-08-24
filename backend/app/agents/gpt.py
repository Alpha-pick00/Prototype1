from openai import AsyncOpenAI

from ..config import settings
from .base import build_recommend_prompt, parse_json_object

# 이 모듈이 담당하는 에이전트 슬롯은 스키마/프론트엔드/테스트 전반에서
# agent="gpt"로 식별된다(파일명·함수명도 그대로) - 하지만 실제로 호출하는
# 모델은 Qwen이다. DashScope가 OpenAI SDK와 호환되는 엔드포인트를 제공해서,
# openai SDK를 base_url만 바꿔 그대로 쓴다(agents/deepseek.py와 동일한
# 패턴). agent="gpt" 식별자 자체를 "qwen"으로 바꾸지 않은 이유 - AgentName
# 리터럴, DB에 저장된 과거 기록, 프론트엔드 타입, 테스트 픽스처 등 수십
# 곳에 걸쳐 있어 그 리네임 자체가 훨씬 큰 변경이 된다. 사용자에게 보이는
# 이름만 frontend/src/app/components/SearchResults.tsx의 AGENT_LABEL에서
# "Qwen"으로 바꿔뒀다.


def _client() -> AsyncOpenAI:
    # max_retries=0 - 사용자 요청(2026-08-15: "너무
    # 느려 더 빠르게"). 실패해도 호출부가 이미 폴백을 갖고 있어 SDK 재시도로
    # 얻는 이득보다 지연 비용이 크다.
    return AsyncOpenAI(api_key=settings.qwen_api_key, base_url=settings.qwen_api_base, max_retries=0)


# qwen3.7-plus는 DashScope 쪽에서 기본적으로 "thinking mode"(내부 추론 과정을
# 다 생성한 뒤에야 응답을 반환)가 켜져 있다(2026-08-20 실측 - enable_thinking을
# 안 주면 단순 질문 하나에도 20~95초가 걸린다, extra_body={"enable_thinking":
# False}를 주면 2초로 줄어든다). 이 프로젝트가 쓰는 프롬프트는 전부 JSON 한
# 덩어리만 필요해서 추론 과정 자체가 필요 없다 - 이 모듈의 모든 호출에 끈다.
_DISABLE_THINKING = {"enable_thinking": False}


# "다른 후보" 카드가 최대 몇 개까지 노출되는지(frontend/.../SearchResults.tsx의
# MAX_OTHER_PROPOSALS)와 맞춰, 그만큼의 후보에 대해서만 개별 이유(notes)를
# 요청한다 - 후보가 90개까지 갈 수 있는 드릴다운 검색에서 전부에 대해
# 이유를 받으면 토큰 낭비다. 최종 선택(index)이 이 범위 밖으로 나가도
# 상관없다 - 그 경우 메인 카드는 별도의 reasoning 필드를 쓴다.
_MAX_CANDIDATE_NOTES = 5


async def recommend_best(query: str, candidates: list[dict]) -> tuple[int, str, dict[int, str]] | None:
    """11번가 검증 후보(app.debate.run_elevenst_only_debate) 중 가장 추천할
    만한 것을 LLM이 고른다 - 가격만이 아니라 리뷰 수/구매만족도까지 본다.
    실패(키 없음·API 오류·범위 밖 index)하면 None - 호출부가 최저가 규칙
    기반으로 폴백한다.

    반환하는 dict(notes)는 "다른 후보"로도 노출될 수 있는 상위 후보들 각각에
    대한 1문장 이유다(2026-08-24 사용자 요청 - "후보 추천해주는 애들도
    추천하는 이유가 있으면 좋겠다") - 지금까지는 전부 같은 문구("관련도순 -
    함께 볼만한 상품")를 썼는데, 클릭해보면 실질적인 설명이 없는 것처럼
    느껴진다는 피드백이었다."""
    if not candidates:
        return None
    note_indices = list(range(min(_MAX_CANDIDATE_NOTES, len(candidates))))
    try:
        client = _client()
        response = await client.chat.completions.create(
            model=settings.qwen_model,
            messages=[{"role": "user", "content": build_recommend_prompt(query, candidates, note_indices)}],
            response_format={"type": "json_object"},
            extra_body=_DISABLE_THINKING,
        )
        data = parse_json_object(response.choices[0].message.content or "")
        index = int(data.get("index"))
        if not (0 <= index < len(candidates)):
            return None
        notes: dict[int, str] = {}
        for key, value in (data.get("notes") or {}).items():
            try:
                note_index = int(key)
            except (TypeError, ValueError):
                continue
            if 0 <= note_index < len(candidates) and value:
                notes[note_index] = str(value).strip()
        return index, str(data.get("reasoning") or "").strip(), notes
    except Exception:
        return None
