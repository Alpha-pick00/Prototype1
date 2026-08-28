"""1000개 재실행(current_1000_v2.jsonl - 수량 가드/브랜드 우선순위/다나와
보조 검증 세 가지 수정이 모두 반영된 버전)에서 _product_name_matches가
False로 채점한 케이스를 DeepSeek로 재채점한다. rescore_relevance.py와
달리, "다른 상품"으로 판정된 진짜 오매칭에 한해 원인 유형(브랜드/수량·용량/
카테고리/본품·부속품/기타)까지 함께 분류한다 - 이번 세션에서 적용한 세
수정이 각 유형을 얼마나 줄였는지 이전 실행(rescore_1000.jsonl)과 직접
비교하기 위함."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.agents import deepseek  # noqa: E402
from app.agents.base import parse_json_object  # noqa: E402

SRC = BACKEND_DIR / "benchmark" / "results" / "current_1000_v2.jsonl"
OUT = BACKEND_DIR / "benchmark" / "results" / "rescore_1000_v2.jsonl"

_PROMPT_TEMPLATE = """당신은 한국 이커머스 검색 품질 평가자입니다. 사용자가 입력한 검색어와
실제로 채택된 상품명을 보고, 이 상품이 사용자가 찾던 것과 "같은 상품"인지 판정하세요.

브랜드/모델/스펙(용량, 사이즈, 색상 등) 표기 차이(영문/한글, 띄어쓰기, 단위 표기
"기가" vs "GB" 등)는 같은 상품으로 간주합니다. 완전히 다른 카테고리, 다른 용도,
본품 대신 부속품/액세서리, 다른 브랜드/모델이 채택된 경우만 "다른 상품"으로
판정하세요.

"다른 상품"이면 mismatch_type을 아래 중 하나로 분류하세요:
- brand: 브랜드/제조사가 다름
- quantity: 브랜드/제품군은 맞지만 수량/용량/사이즈가 명시적으로 다름
- category: 완전히 다른 카테고리/용도의 상품
- accessory: 본품을 찾는데 부속품/소모품이 채택됨(또는 그 반대)
- other: 위 어디에도 안 맞는 경우

검색어: {query}
채택된 상품명: {product_name}

JSON으로만 답하세요:
{{"same_product": true 또는 false, "mismatch_type": "brand|quantity|category|accessory|other|null", "reason": "한 문장 이유"}}"""


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
            mismatch_type = data.get("mismatch_type") if same_product is False else None
            reason = data.get("reason", "")
        except Exception as exc:
            same_product = None
            mismatch_type = None
            reason = f"ERROR: {exc}"

        return {
            "id": item["id"],
            "category": item["category"],
            "query": item["query"],
            "decision_product_name": item["decision_product_name"],
            "llm_same_product": same_product,
            "mismatch_type": mismatch_type,
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
    diff = [r for r in results if r["llm_same_product"] is False]
    unknown = sum(1 for r in results if r["llm_same_product"] is None)
    print(f"\n같은 상품(채점 함수 오탐): {same}")
    print(f"다른 상품(진짜 오매칭): {len(diff)}")
    print(f"판정 실패: {unknown}")

    from collections import Counter

    type_counter = Counter(r["mismatch_type"] for r in diff)
    print("\n오매칭 유형별:")
    for t, cnt in type_counter.most_common():
        print(f"  {t}: {cnt}")

    print(f"\n저장: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
