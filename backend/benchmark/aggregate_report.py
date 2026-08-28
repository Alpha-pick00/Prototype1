"""baseline vs current 벤치마크 결과 집계 리포트(2026-08-28). 표준 라이브러리만
사용(pandas 없음) - 두 워크트리 어디서나 실행 가능하게. 벤치마크 종료 후
되돌릴 임시 도구."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def load_results(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    latencies = [r["latency_ms"] for r in ok if r["latency_ms"] is not None]
    matches = [r["relevance_match"] for r in ok if r["relevance_match"] is not None]

    node_totals: dict[str, dict[str, int]] = defaultdict(lambda: {"prompt": 0, "completion": 0, "count": 0})
    total_prompt = 0
    total_completion = 0
    for r in ok:
        for u in r.get("usage", []):
            node = u.get("node", "unknown")
            p = u.get("prompt_tokens") or 0
            c = u.get("completion_tokens") or 0
            node_totals[node]["prompt"] += p
            node_totals[node]["completion"] += c
            node_totals[node]["count"] += 1
            total_prompt += p
            total_completion += c

    return {
        "total": len(results),
        "ok": len(ok),
        "failed": len(failed),
        "avg_latency_ms": round(statistics.mean(latencies), 1) if latencies else None,
        "p50_latency_ms": round(percentile(latencies, 0.5), 1) if latencies else None,
        "p95_latency_ms": round(percentile(latencies, 0.95), 1) if latencies else None,
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "avg_total_tokens_per_request": round((total_prompt + total_completion) / len(ok), 1) if ok else None,
        "relevance_match_rate": round(100 * sum(matches) / len(matches), 1) if matches else None,
        "node_totals": dict(node_totals),
    }


def summarize_by_category(results: list[dict]) -> dict[str, dict]:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    return {cat: summarize(rows) for cat, rows in sorted(by_cat.items())}


def format_report(baseline_summary: dict, current_summary: dict, baseline_by_cat: dict, current_by_cat: dict) -> str:
    lines = []
    lines.append("# 베이스라인(challenge/judge) vs 현재(raw 모드) 벤치마크 리포트\n")

    lines.append("## 전체 요약\n")
    lines.append("| 지표 | Baseline | Current | 차이 |")
    lines.append("|---|---|---|---|")

    def row(label, b, c, fmt="{}"):
        b_str = fmt.format(b) if b is not None else "N/A"
        c_str = fmt.format(c) if c is not None else "N/A"
        diff = ""
        if isinstance(b, (int, float)) and isinstance(c, (int, float)):
            diff = fmt.format(round(c - b, 1))
        lines.append(f"| {label} | {b_str} | {c_str} | {diff} |")

    row("요청 수 (성공/전체)", f"{baseline_summary['ok']}/{baseline_summary['total']}", f"{current_summary['ok']}/{current_summary['total']}")
    row("평균 레이턴시 (ms)", baseline_summary["avg_latency_ms"], current_summary["avg_latency_ms"])
    row("p50 레이턴시 (ms)", baseline_summary["p50_latency_ms"], current_summary["p50_latency_ms"])
    row("p95 레이턴시 (ms)", baseline_summary["p95_latency_ms"], current_summary["p95_latency_ms"])
    row("총 prompt 토큰", baseline_summary["total_prompt_tokens"], current_summary["total_prompt_tokens"])
    row("총 completion 토큰", baseline_summary["total_completion_tokens"], current_summary["total_completion_tokens"])
    row("요청당 평균 총 토큰", baseline_summary["avg_total_tokens_per_request"], current_summary["avg_total_tokens_per_request"])
    row("관련성 매칭률 (%)", baseline_summary["relevance_match_rate"], current_summary["relevance_match_rate"])

    lines.append("\n> 관련성 매칭률은 `_product_name_matches`(rapidfuzz 기반, current 워크트리 버전으로")
    lines.append("> 고정)로 자동 채점한 구조적 관련성이다. 이 함수는 원래 AI 상세검색 facet 값처럼")
    lines.append("> 표기가 비교적 가까운 문자열 비교용으로 설계돼, 사용자가 자유롭게 입력한 검색어와")
    lines.append("> 실제 상품명(단위 표기 \"기가\" vs \"GB\", 영문 대소문자/붙여쓰기 차이 등)을 비교할 때는")
    lines.append("> 사람이 보기엔 명백히 맞는 매칭도 임계값 미달로 False가 나올 수 있다 - 절대")
    lines.append("> 매칭률 수치보다 baseline과 current 사이의 상대적 차이에 더 무게를 둬야 한다(두")
    lines.append("> 버전에 동일한 채점 기준이 적용됐으므로 상대 비교는 공정하다).\n")

    lines.append("## 노드별 토큰 세부\n")
    lines.append("| 노드 | Baseline 호출수 | Baseline 총prompt | Baseline 총completion | Current 호출수 | Current 총prompt | Current 총completion |")
    lines.append("|---|---|---|---|---|---|---|")
    all_nodes = sorted(set(baseline_summary["node_totals"]) | set(current_summary["node_totals"]))
    for node in all_nodes:
        b = baseline_summary["node_totals"].get(node, {"count": 0, "prompt": 0, "completion": 0})
        c = current_summary["node_totals"].get(node, {"count": 0, "prompt": 0, "completion": 0})
        lines.append(f"| {node} | {b['count']} | {b['prompt']} | {b['completion']} | {c['count']} | {c['prompt']} | {c['completion']} |")
    lines.append("\n> current에서 challenge/judge 행이 0인 것은 버그가 아니라 raw 모드의 설계 그대로다")
    lines.append("> (challenge/judge LLM 호출 자체를 스킵) - 이번 벤치마크가 검증하려는 아키텍처")
    lines.append("> 변화 그 자체를 정확히 반영한다.\n")

    lines.append("## 카테고리별 세부\n")
    lines.append("| 카테고리 | Baseline 매칭률(%) | Current 매칭률(%) | Baseline 평균지연(ms) | Current 평균지연(ms) |")
    lines.append("|---|---|---|---|---|")
    all_cats = sorted(set(baseline_by_cat) | set(current_by_cat))
    for cat in all_cats:
        b = baseline_by_cat.get(cat, {})
        c = current_by_cat.get(cat, {})
        lines.append(
            f"| {cat} | {b.get('relevance_match_rate', 'N/A')} | {c.get('relevance_match_rate', 'N/A')} | "
            f"{b.get('avg_latency_ms', 'N/A')} | {c.get('avg_latency_ms', 'N/A')} |"
        )

    lines.append("\n## 실패 건수\n")
    lines.append(f"- Baseline: {baseline_summary['failed']}건 실패 / {baseline_summary['total']}건")
    lines.append(f"- Current: {current_summary['failed']}건 실패 / {current_summary['total']}건")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    baseline_results = load_results(args.baseline)
    current_results = load_results(args.current)

    baseline_summary = summarize(baseline_results)
    current_summary = summarize(current_results)
    baseline_by_cat = summarize_by_category(baseline_results)
    current_by_cat = summarize_by_category(current_results)

    report = format_report(baseline_summary, current_summary, baseline_by_cat, current_by_cat)
    print(report)

    out_path = args.out or (args.baseline.parent / f"report_{args.baseline.stem}_vs_{args.current.stem}.md")
    out_path.write_text(report, encoding="utf-8")
    print(f"\n리포트 저장: {out_path}")


if __name__ == "__main__":
    main()
