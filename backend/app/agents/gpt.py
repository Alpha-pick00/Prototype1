import json
import re

from openai import AsyncOpenAI

from ..config import settings
from .base import build_candidate_notes_prompt, build_recommend_prompt, build_refine_query_prompt, parse_json_object

# 이 모듈이 담당하는 에이전트 슬롯은 스키마/프론트엔드/테스트 전반에서
# agent="gpt"로 식별된다(파일명·함수명도 그대로) - 원래 실제로 호출하는
# 모델은 Qwen이었다(DashScope가 OpenAI SDK와 호환되는 엔드포인트를 제공해서,
# openai SDK를 base_url만 바꿔 그대로 쓰는 방식 - agents/deepseek.py와 동일한
# 패턴). agent="gpt" 식별자 자체를 "qwen"으로 바꾸지 않은 이유 - AgentName
# 리터럴, DB에 저장된 과거 기록, 프론트엔드 타입, 테스트 픽스처 등 수십
# 곳에 걸쳐 있어 그 리네임 자체가 훨씬 큰 변경이 된다. 사용자에게 보이는
# 이름만 frontend/src/app/components/SearchResults.tsx의 AGENT_LABEL에서
# "Qwen"으로 바꿔뒀다.
#
# 2026-08-25부터 이 슬롯은 의도적으로 HCX(CLOVA Studio)를 호출한다(처음엔
# Qwen(DashScope) 쿼터 소진 때문에 임시로 바꾼 것이었으나, 이후 쿼터가
# 복구된 뒤에도 한국어 표현 이해도와 효용성이 더 낫다고 판단해 HCX를 그대로
# 유지하기로 결정했다 - Qwen으로 되돌리는 건 후속 과제가 아니다).
# agent="gpt" 식별자·함수 시그니처는 그대로 두고 _client()/모델만 HCX로
# 갈아 끼웠다. response_format={"type": "json_object"}는 HCX의 OpenAI 호환
# 엔드포인트가 지원 안 해 빼고(agents/hcx.py 주석 참고), 프롬프트 지시 +
# parse_json_object 파싱으로 대신한다. extra_body=_DISABLE_THINKING(DashScope
# 전용 파라미터)도 HCX엔 없어 안 쓴다. embeddings.py(관련도 정렬·의미 유사도
# 구제)는 이 전환과 무관하게 계속 Qwen을 쓴다.


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
    # 2026-08-25부터 의도적으로 HCX(CLOVA Studio)를 가리킨다(위 모듈
    # docstring 참고 - 한국어 이해도/효용성 때문에 유지, 임시 조치 아님).
    global _client_instance
    if _client_instance is None:
        _client_instance = AsyncOpenAI(api_key=settings.hcx_api_key, base_url=settings.hcx_api_base, max_retries=0)
    return _client_instance


# qwen3.7-plus는 DashScope 쪽에서 기본적으로 "thinking mode"(내부 추론 과정을
# 다 생성한 뒤에야 응답을 반환)가 켜져 있다(2026-08-20 실측 - enable_thinking을
# 안 주면 단순 질문 하나에도 20~95초가 걸린다, extra_body={"enable_thinking":
# False}를 주면 2초로 줄어든다). 이 프로젝트가 쓰는 프롬프트는 전부 JSON 한
# 덩어리만 필요해서 추론 과정 자체가 필요 없다 - Qwen을 다시 쓰게 되면 이
# 모듈의 세 호출 모두에 extra_body=_DISABLE_THINKING을 다시 붙일 것(HCX엔
# 없는 DashScope 전용 파라미터라 지금은 안 쓴다).
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
            model=settings.hcx_recommend_model,
            messages=[{"role": "user", "content": build_refine_query_prompt(stripped)}],
        )
        data = parse_json_object(response.choices[0].message.content or "")
        refined = str(data.get("query") or "").strip()
        return refined or None
    except Exception:
        return None


async def recommend_best(
    query: str, candidates: list[dict], excluded_grade_tokens: list[str] | None = None
) -> tuple[int, str] | None:
    """11번가 검증 후보(app.debate.run_elevenst_only_debate) 중 가장 추천할
    만한 것을 LLM이 고른다 - 가격만이 아니라 리뷰 수/구매만족도까지 본다.
    실패(키 없음·API 오류·범위 밖 index)하면 None - 호출부가 최저가 규칙
    기반으로 폴백한다.

    2026-08-24: 후보별 개별 이유(candidate_notes)를 처음엔 이 호출 응답에
    같이 얹었는데(출력 토큰이 늘어 평균 3.2초 -> 10.3초, 최대 20초까지 튐 -
    직접 실측), index 선택과 개별 이유 생성은 서로 의존하지 않는 별개
    작업이라 별도 함수(candidate_notes)로 분리했다 - app.debate가 이 함수와
    asyncio.gather로 동시에 호출해, 전체 대기시간이 두 호출의 합이 아니라
    더 느린 쪽 하나로 수렴하게 한다.

    excluded_grade_tokens(2026-08-26) - _search_and_rank_candidates가 이미
    이 토큰이 없는 후보를 앞으로 우대해뒀지만, 이 함수는 순서와 무관하게
    자유롭게 고르는 별도 LLM이라 프롬프트에도 "[다른 등급]" 표시로 같은
    정보를 넘겨야 실제로 반영된다(adk_pipeline.py의 judge와 동일한 이유)."""
    if not candidates:
        return None
    try:
        client = _client()
        response = await client.chat.completions.create(
            model=settings.hcx_recommend_model,
            messages=[
                {
                    "role": "user",
                    "content": build_recommend_prompt(query, candidates, excluded_grade_tokens),
                }
            ],
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
            model=settings.hcx_recommend_model,
            temperature=0,
            messages=[{"role": "user", "content": build_candidate_notes_prompt(query, candidates, note_indices)}],
        )
        text = response.choices[0].message.content or ""
        notes = _parse_candidate_notes(text, candidates)
        return notes
    except Exception:
        return {}


_REASON_KEYS = ("reason", "note", "이유", "설명")
_NAME_KEYS = ("name", "product_name", "title", "상품명")
_INDEX_KEYS = ("index", "후보", "candidate", "idx")
_INDEX_IN_BRACKETS_RE = re.compile(r"\d+")


def _extract_json_value(text: str) -> object | None:
    """text 안에서 JSON 값(객체 또는 배열)을 최대한 관대하게 뽑아낸다 -
    먼저 최상위가 배열인지({...} 앞에 [가 먼저 나오는지)로 배열/객체를
    구분하고, 실패하면 숫자 내 콤마(2026-08-27 실측 - "price": 1,251,300
    처럼 HCX가 JSON 문법을 깨뜨린 사례) 등 흔한 오류를 복구해 재시도한다."""
    stripped = text.strip()
    obj_start = stripped.find("{")
    arr_start = stripped.find("[")
    if arr_start != -1 and (obj_start == -1 or arr_start < obj_start):
        match = re.search(r"\[.*\]", stripped, re.DOTALL)
    else:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not match:
        return None
    raw = match.group(0)
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        repaired = re.sub(r"(?<=\d),(?=\d{3}\D)", "", raw)
        try:
            return json.loads(repaired)
        except (ValueError, TypeError):
            return None


def _find_reason_for_name(value: object, name: str) -> str | None:
    """파싱된 JSON 구조를 재귀적으로 훑어, 후보 상품명(name)과 정확히
    일치하는 key 옆의 문자열 값이나, "reason"류 필드가 붙어있는 객체를
    찾아 이유 문자열을 돌려준다."""
    if isinstance(value, dict):
        for key, val in value.items():
            if key == name and isinstance(val, str) and val.strip():
                return val.strip()
            if key in _NAME_KEYS and isinstance(val, str) and val.strip() == name:
                for reason_key in _REASON_KEYS:
                    reason = value.get(reason_key)
                    if isinstance(reason, str) and reason.strip():
                        return reason.strip()
        for val in value.values():
            found = _find_reason_for_name(val, name)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_reason_for_name(item, name)
            if found:
                return found
    return None


def _index_and_reason_from_item(item: dict) -> tuple[int, str] | None:
    """리스트 항목 하나에서 (index, reason)을 뽑는다 - 인덱스 필드값이
    "[0]"처럼 대괄호로 감싸져 오는 경우(2026-08-27 실측, HCX가 "후보":
    "[0]" 형태로 응답)까지 포함해 첫 번째 숫자를 index로 쓴다."""
    index_val = next((item[k] for k in _INDEX_KEYS if k in item), None)
    if index_val is None:
        return None
    match = _INDEX_IN_BRACKETS_RE.search(str(index_val))
    if not match:
        return None
    index = int(match.group(0))
    reason = next((item[k] for k in _REASON_KEYS if k in item), None)
    if not isinstance(reason, str) or not reason.strip():
        return None
    return index, reason.strip()


def _parse_candidate_notes(text: str, candidates: list[dict]) -> dict[int, str]:
    """지시한 형식({"notes": {"0": "...", ...}})을 우선 시도한다 - index
    기반이라 상품명 매칭 없이 그대로 후보에 대응시킬 수 있어 가장
    신뢰도가 높다. HCX가 형식을 안 지키는 경우(2026-08-27 실측으로 여러
    다른 변형을 확인했다 - 리스트+name/reason 필드, 최상위 {"상품명":
    "이유문장"} dict, 리스트+{"후보": "[0]", "설명": "..."} 등)를 대비해
    두 단계 폴백을 쓴다: (1) 리스트 항목이 인덱스+이유 필드 쌍으로
    보이면 그대로 매핑, (2) 그마저 안 맞으면 파싱된 JSON 구조를 재귀
    탐색해 후보 상품명과 일치하는 이유를 찾는다 - 매번 새로 나오는
    변형마다 개별 분기를 추가하는 대신, "어떤 구조든 인덱스나 상품명
    근처에 이유 문자열이 있을 것"이라는 더 느슨하지만 견고한 가정으로
    대응한다."""
    data = _extract_json_value(text)
    notes: dict[int, str] = {}
    if isinstance(data, dict) and isinstance(data.get("notes"), dict):
        for key, value in data["notes"].items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(candidates) and value:
                notes[index] = str(value).strip()
        if notes:
            return notes

    if data is None:
        return notes

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            result = _index_and_reason_from_item(item)
            if result is None:
                continue
            index, reason = result
            if 0 <= index < len(candidates):
                notes[index] = reason
        if notes:
            return notes

    for index, candidate in enumerate(candidates):
        reason = _find_reason_for_name(data, candidate["product_name"])
        if reason:
            notes[index] = reason
    return notes
