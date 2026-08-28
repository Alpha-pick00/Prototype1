"""1000개 벤치마크에서 _product_name_matches가 False로 채점한 케이스를
DeepSeek로 재채점해, 실제로 무관한 상품(진짜 회귀)인지 채점 함수의 표기차
오탐인지 분류한다. 검색 품질 개선 실험의 사전 조사용 - 결과는
results/rescore_1000.jsonl에 저장한다."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.agents import deepseek  # noqa: E402
from app.agents.base import parse_json_object  # noqa: E402

SRC = BACKEND_DIR / "benchmark" / "results" / "current_1000.jsonl"
OUT = BACKEND_DIR / "benchmark" / "results" / "rescore_1000.jsonl"

_PROMPT_TEMPLATE = """당신은 한국 이커머스 검색 품질 평가자입니다. 사용자가 입력한 검색어와
실제로 채택된 상품명을 보고, 이 상품이 사용자가 찾던 것과 "같은 상품"인지 판정하세요.

브랜드/모델/스펙(용량, 사이즈, 색상 등) 표기 차이(영문/한글, 띄어쓰기, 단위 표기
"기가" vs "GB" 등)는 같은 상품으로 간주합니다. 완전히 다른 카테고리, 다른 용도,
본품 대신 부속품/액세서리, 다른 브랜드/모델이 채택된 경우만 "다른 상품"으로
판정하세요.

검색어: {query}
채택된 상품명: {product_name}

JSON으로만 답하세요: {{"same_product": true 또는 false, "reason": "한 문장 이유"}}"""


async def rescore_one(item: dict, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        prompt = _PROMPT_TEMPLATE.format(query=item["query"], product_name=item["decision_product_name"])
        try:
            client = deepseek._client()
            response = await client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
            )
            data = parse_json_object(response.choices[0].message.content or "")
            same_product = data.get("same_product")
            reason = data.get("reason", "")
        except Exception as exc:
            same_product = None
            reason = f"ERROR: {exc}"

        return {
            "id": item["id"],
            "category": item["category"],
            "query": item["query"],
            "decision_product_name": item["decision_product_name"],
            "llm_same_product": same_product,
            "llm_reason": reason,
        }


async def main() -> None:
    fails = []
    with SRC.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r["status"] == "ok" and r["relevance_match"] is False:
                fails.append(r)

    print(f"재채점 대상: {len(fails)}건")
    semaphore = asyncio.Semaphore(10)
    tasks = [rescore_one(item, semaphore) for item in fails]
    results = []
    done = 0
    for coro in asyncio.as_completed(tasks):
        r = await coro
        results.append(r)
        done += 1
        if done % 50 == 0:
            print(f"{done}/{len(fails)}")

    with OUT.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    same = sum(1 for r in results if r["llm_same_product"] is True)
    diff = sum(1 for r in results if r["llm_same_product"] is False)
    unknown = sum(1 for r in results if r["llm_same_product"] is None)
    print(f"\n같은 상품(채점 함수 오탐): {same}")
    print(f"다른 상품(진짜 오매칭): {diff}")
    print(f"판정 실패: {unknown}")
    print(f"저장: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
