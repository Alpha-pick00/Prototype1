"""baseline(f148b9f, challenge/judge 실호출) vs current(raw 모드) 라이브 벤치마크
실행기(2026-08-28). 한 포트에 데이터셋을 bounded concurrency로 순차 실행하고,
결과를 jsonl+csv로 저장한다. 벤치마크 종료 후 되돌릴 임시 도구 - 프로덕션
코드가 아니다."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent

# 채점 기준(_product_name_matches)은 항상 current(main-local, 여기 이 파일이
# 있는 워크트리)의 price_table.py로 고정한다 - baseline 워크트리에서 이
# 스크립트를 실행해도 채점 로직만은 current 걸 쓴다(baseline의 매칭 함수는
# 표기 차이 보정이 없는 더 오래된 버전이라, 실행 위치에 따라 채점 기준이
# 달라지면 두 버전의 relevance_match를 나란히 비교하는 의미가 없어진다).
sys.path.insert(0, str(BACKEND_DIR))
from app.price_table import _product_name_matches  # noqa: E402


def load_dataset(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_usage_log(path: Path) -> dict[str, dict]:
    """usage_log.jsonl 전체를 request_id -> row로 인덱싱한다. 벤치마크 시작
    시점에 한 번 읽고, 각 요청 직후 다시 읽어 최신 라인까지 반영한다(로컬
    파일이라 tail 비용이 적음, 1000개 규모에서도 파일 크기가 작아 매번
    전체를 다시 읽어도 무리 없음)."""
    if not path.exists():
        return {}
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows[row.get("request_id", "")] = row
    return rows


async def run_one(
    client: httpx.AsyncClient,
    item: dict,
    base_url: str,
    version: str,
    usage_log_path: Path,
    semaphore: asyncio.Semaphore,
) -> dict:
    result = {
        "id": item["id"],
        "category": item["category"],
        "query": item["query"],
        "version": version,
        "status": "ok",
        "http_status": None,
        "latency_ms": None,
        "decision_product_name": None,
        "relevance_match": None,
        "usage": [],
        "error": None,
    }
    async with semaphore:
        t0 = time.monotonic()
        try:
            resp = await client.post(f"{base_url}/decide", json={"query": item["query"]}, timeout=120.0)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # 1회 재시도 - 외부 LLM API 일시 오류 대비. 배치 전체를 죽이지 않는다.
            try:
                resp = await client.post(f"{base_url}/decide", json={"query": item["query"]}, timeout=120.0)
            except Exception as exc2:
                result["status"] = "error"
                result["error"] = f"{type(exc2).__name__}: {exc2}"
                result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
                return result
        result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        result["http_status"] = resp.status_code

        if resp.status_code != 200:
            result["status"] = "error"
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return result

        try:
            body = resp.json()
            decision = body.get("decision") or {}
            product_name = decision.get("product_name")
            result["decision_product_name"] = product_name
            if product_name:
                result["relevance_match"] = bool(_product_name_matches(item["query"], product_name))
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"response parse failed: {exc}"
            return result

        request_id = resp.headers.get("X-Request-Id")
        if request_id:
            # 응답을 이미 받았으므로 usage_log.jsonl에는 이 요청의 finish_request가
            # append를 마친 뒤다(main.py에서 finally 블록이 응답 반환 전에 실행됨).
            usage_rows = load_usage_log(usage_log_path)
            row = usage_rows.get(request_id)
            if row:
                result["usage"] = row.get("usage", [])

    return result


async def run_benchmark(
    dataset_path: Path,
    base_url: str,
    version: str,
    usage_log_path: Path,
    concurrency: int,
    out_path: Path,
) -> list[dict]:
    dataset = load_dataset(dataset_path)
    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict] = []

    async with httpx.AsyncClient() as client:
        tasks = [
            run_one(client, item, base_url, version, usage_log_path, semaphore) for item in dataset
        ]
        total = len(tasks)
        done = 0
        for coro in asyncio.as_completed(tasks):
            r = await coro
            results.append(r)
            done += 1
            print(
                f"[{version}] {done}/{total} {r['id']} status={r['status']} "
                f"latency={r['latency_ms']}ms match={r['relevance_match']}"
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    summary_path = out_path.with_name(out_path.stem + "_summary.csv")
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["id", "category", "query", "version", "status", "latency_ms", "total_prompt_tokens", "total_completion_tokens", "relevance_match", "decision_product_name"]
        )
        for r in results:
            total_prompt = sum(u.get("prompt_tokens") or 0 for u in r["usage"])
            total_completion = sum(u.get("completion_tokens") or 0 for u in r["usage"])
            writer.writerow(
                [r["id"], r["category"], r["query"], r["version"], r["status"], r["latency_ms"], total_prompt, total_completion, r["relevance_match"], r["decision_product_name"]]
            )

    print(f"\n결과 저장: {out_path}")
    print(f"요약 저장: {summary_path}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--version", required=True, choices=["baseline", "current"])
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--usage-log", type=Path, default=None, help="usage_log.jsonl 경로(기본: 해당 워크트리의 backend/data/usage_log.jsonl)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    base_url = f"http://127.0.0.1:{args.port}"
    usage_log_path = args.usage_log or (BACKEND_DIR / "data" / "usage_log.jsonl")
    dataset_size = sum(1 for _ in args.dataset.open(encoding="utf-8") if _.strip())
    out_path = args.out or (
        BACKEND_DIR / "benchmark" / "results" / f"{args.version}_{dataset_size}_{int(time.time())}.jsonl"
    )

    print(f"버전: {args.version} | 포트: {args.port} | 데이터셋: {args.dataset} ({dataset_size}개) | 동시성: {args.concurrency}")
    print(f"usage 로그: {usage_log_path}")

    asyncio.run(run_benchmark(args.dataset, base_url, args.version, usage_log_path, args.concurrency, out_path))


if __name__ == "__main__":
    main()
