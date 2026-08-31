"""run_benchmark.py의 이어받기(resume) 래퍼 - Groq 하루 토큰 한도(TPD)에 걸려
중간에 끊기는 baseline(디베이트+challenge+judge) 라이브 벤치마크를 여러 날에
나눠 완료하기 위한 임시 도구. run_benchmark.py 자체는 건드리지 않는다.

--out 경로에 이미 결과 파일이 있으면 그중 status=ok인 id는 건너뛰고, 나머지
(TPD 등으로 실패한 id + 아직 시도 안 한 id)만 이번 실행 대상 데이터셋으로
추려서 run_one()을 재사용해 돌린 뒤, 기존 성공분과 합쳐 같은 경로에 다시
저장한다. 여러 날에 걸쳐 몇 번을 실행해도 최종적으로 모든 id가 status=ok가
될 때까지 안전하게 이어받을 수 있다."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCHMARK_DIR))
from run_benchmark import load_dataset, run_one  # noqa: E402

BACKEND_DIR = BENCHMARK_DIR.parent


def load_existing(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rows[row["id"]] = row
    return rows


def write_outputs(results: list[dict], out_path: Path) -> None:
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
            total_prompt = sum(u.get("prompt_tokens") or 0 for u in r.get("usage", []))
            total_completion = sum(u.get("completion_tokens") or 0 for u in r.get("usage", []))
            writer.writerow(
                [r["id"], r["category"], r["query"], r["version"], r["status"], r["latency_ms"], total_prompt, total_completion, r["relevance_match"], r["decision_product_name"]]
            )
    print(f"\n결과 저장: {out_path}")
    print(f"요약 저장: {summary_path}")


async def main_async() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--version", required=True, choices=["baseline", "current"])
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--usage-log", type=Path, default=None)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--min-gap-seconds",
        type=float,
        default=0.0,
        help="요청 사이 최소 대기 시간(초) - Groq 하루 토큰 한도(TPD)를 하루 전체에"
        " 걸쳐 나눠 쓰기 위해 순차 실행 시에만 적용(concurrency=1 전제)",
    )
    args = parser.parse_args()

    import httpx

    base_url = f"http://127.0.0.1:{args.port}"
    usage_log_path = args.usage_log or (BACKEND_DIR / "data" / "usage_log.jsonl")

    full_dataset = load_dataset(args.dataset)
    existing = load_existing(args.out)

    already_ok = {k: v for k, v in existing.items() if v.get("status") == "ok"}
    remaining = [item for item in full_dataset if item["id"] not in already_ok]

    print(f"전체 {len(full_dataset)}개 중 기존 성공 {len(already_ok)}개 재사용, 이번 실행 대상 {len(remaining)}개")

    if not remaining:
        print("모든 id가 이미 status=ok - 재실행할 것이 없다.")
        write_outputs(list(already_ok.values()), args.out)
        return

    merged: dict[str, dict] = dict(already_ok)
    total = len(remaining)

    if args.min_gap_seconds > 0:
        # 순차 실행 + 매 건 즉시 저장 - TPD로 중간에 끊겨도 그때까지 성공분은
        # 파일에 이미 반영돼 있어 손실이 없다.
        async with httpx.AsyncClient() as client:
            semaphore = asyncio.Semaphore(1)
            for i, item in enumerate(remaining, start=1):
                r = await run_one(client, item, base_url, args.version, usage_log_path, semaphore)
                merged[r["id"]] = r
                print(f"[{args.version}] {i}/{total} {r['id']} status={r['status']} latency={r['latency_ms']}ms match={r['relevance_match']}")
                ordered = [merged[it["id"]] for it in full_dataset if it["id"] in merged]
                write_outputs(ordered, args.out)
                if r["status"] != "ok":
                    print(f"  -> 실패({r.get('error', '')[:80]}), 남은 요청 계속 시도")
                if i < total:
                    await asyncio.sleep(args.min_gap_seconds)
    else:
        semaphore = asyncio.Semaphore(args.concurrency)
        new_results: list[dict] = []
        async with httpx.AsyncClient() as client:
            tasks = [run_one(client, item, base_url, args.version, usage_log_path, semaphore) for item in remaining]
            done = 0
            for coro in asyncio.as_completed(tasks):
                r = await coro
                new_results.append(r)
                done += 1
                print(f"[{args.version}] {done}/{total} {r['id']} status={r['status']} latency={r['latency_ms']}ms match={r['relevance_match']}")
        for r in new_results:
            merged[r["id"]] = r
        ordered = [merged[item["id"]] for item in full_dataset if item["id"] in merged]
        write_outputs(ordered, args.out)

    ordered = [merged[item["id"]] for item in full_dataset if item["id"] in merged]
    ok_count = sum(1 for r in ordered if r["status"] == "ok")
    print(f"\n누적 성공: {ok_count}/{len(full_dataset)}")
    if ok_count < len(full_dataset):
        print("아직 실패/미시도 id가 남아있다 - 같은 명령으로 다시 실행하면 이어받는다.")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
