"""벤치마크 3웨이 결과에서 비용(usage_by_node)과 가격 정확도(price_table_offers)
지표를 계산한다(2026-08-31). aggregate_report_3way.py가 다루는 성능 지표
(레이턴시/관련성/Precision-Recall-F1/실패유형)와 별도 축이라 분리된 스크립트로
둔다 - 두 스크립트 모두 표준 라이브러리만 쓰는 임시 도구."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

# Groq/Qwen/DeepSeek/HCX 전부 무료 티어라 실제 과금 단가가 없다 - 참고용으로
# OpenAI 계열 시세 오더를 붙여 세 아키텍처의 "상대적" 토큰 소모량 차이를
# 비용 감각으로 환산한다. 실제 이 프로젝트의 API 비용은 0원(무료 티어)이다.
_REFERENCE_USD_PER_1K_PROMPT = 0.00015
_REFERENCE_USD_PER_1K_COMPLETION = 0.0006
_KRW_PER_USD = 1400


def load_results(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _sum_usage(usage_by_node: dict) -> tuple[int, int]:
    total_prompt = sum(v.get("prompt_tokens") or 0 for v in usage_by_node.values())
    total_completion = sum(v.get("completion_tokens") or 0 for v in usage_by_node.values())
    return total_prompt, total_completion


def cost_summary(results: list[dict]) -> dict:
    rows_with_usage = [r for r in results if r.get("usage_by_node")]
    per_request_totals = [_sum_usage(r["usage_by_node"]) for r in rows_with_usage]
    prompt_tokens = [p for p, _ in per_request_totals]
    completion_tokens = [c for _, c in per_request_totals]

    node_totals: dict[str, dict[str, int]] = {}
    for r in rows_with_usage:
        for node, bucket in r["usage_by_node"].items():
            agg = node_totals.setdefault(node, {"prompt_tokens": 0, "completion_tokens": 0, "count": 0})
            agg["prompt_tokens"] += bucket.get("prompt_tokens") or 0
            agg["completion_tokens"] += bucket.get("completion_tokens") or 0
            agg["count"] += 1

    total_prompt = sum(prompt_tokens)
    total_completion = sum(completion_tokens)
    avg_prompt = round(statistics.mean(prompt_tokens), 1) if prompt_tokens else None
    avg_completion = round(statistics.mean(completion_tokens), 1) if completion_tokens else None
    avg_total = round(avg_prompt + avg_completion, 1) if (avg_prompt is not None and avg_completion is not None) else None

    ref_cost_usd_per_request = None
    if avg_prompt is not None:
        ref_cost_usd_per_request = round(
            avg_prompt / 1000 * _REFERENCE_USD_PER_1K_PROMPT
            + avg_completion / 1000 * _REFERENCE_USD_PER_1K_COMPLETION,
            5,
        )

    return {
        "requests_with_usage": len(rows_with_usage),
        "total_requests": len(results),
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "avg_prompt_tokens_per_request": avg_prompt,
        "avg_completion_tokens_per_request": avg_completion,
        "avg_total_tokens_per_request": avg_total,
        "ref_cost_usd_per_request": ref_cost_usd_per_request,
        "ref_cost_krw_per_request": round(ref_cost_usd_per_request * _KRW_PER_USD, 2) if ref_cost_usd_per_request is not None else None,
        "node_totals": node_totals,
    }


_PRICE_NUM_RE = re.compile(r"[\d,]+")


def _parse_price_krw(text: str | None) -> int | None:
    """"1,887,550원" 같은 문자열에서 정수 원화를 뽑는다. 옵션 문자열("가격 정보
    없음" 등)이면 None."""
    if not text:
        return None
    match = _PRICE_NUM_RE.search(text)
    if not match:
        return None
    digits = match.group(0).replace(",", "")
    return int(digits) if digits else None


def price_accuracy_summary(results: list[dict]) -> dict:
    """추천 가격이 표시된 최저가 대비 몇 % 차이 나는지 계산한다. price_table
    (다나와 판매처별 실측가)이 있는 아키텍처에서만 계산 가능 - 11번가 단일
    소스 아키텍처(1st/2nd Proposed)는 price_table 자체가 없어 이 지표가
    구조적으로 N/A다."""
    deltas: list[float] = []
    exact_matches = 0
    evaluated = 0
    for r in results:
        offers = r.get("price_table_offers")
        decision_price = _parse_price_krw(r.get("decision_price"))
        if not offers or decision_price is None:
            continue
        offer_prices = [o.get("price_krw") for o in offers if o.get("price_krw") is not None]
        if not offer_prices:
            continue
        lowest = min(offer_prices)
        if lowest <= 0:
            continue
        evaluated += 1
        delta_pct = round(100 * (decision_price - lowest) / lowest, 2)
        deltas.append(delta_pct)
        if decision_price == lowest:
            exact_matches += 1

    return {
        "evaluated": evaluated,
        "exact_lowest_match": exact_matches,
        "exact_lowest_match_rate": round(100 * exact_matches / evaluated, 1) if evaluated else None,
        "avg_delta_pct": round(statistics.mean(deltas), 2) if deltas else None,
        "median_delta_pct": round(statistics.median(deltas), 2) if deltas else None,
        "max_delta_pct": round(max(deltas), 2) if deltas else None,
    }


def format_report(labels: tuple[str, str, str], costs: tuple[dict, dict, dict], prices: tuple[dict, dict, dict]) -> str:
    l1, l2, l3 = labels
    c1, c2, c3 = costs
    p1, p2, p3 = prices
    lines = []
    lines.append(f"# {l1} vs {l2} vs {l3} — 비용·가격정확도 리포트\n")

    lines.append("## 비용(토큰 사용량)\n")
    lines.append(
        "> 세 프로바이더(Groq/Qwen/DeepSeek/HCX) 모두 무료 티어라 실제 과금은 없다. "
        "여기 원화·달러 환산은 OpenAI 계열 시세를 참고 단가로 붙여 '상대적 비용 감각'을 "
        "보여주기 위한 것으로, 실제 청구 금액이 아니다.\n"
    )
    lines.append(f"| 지표 | {l1} | {l2} | {l3} |")
    lines.append("|---|---|---|---|")

    def row(label, v1, v2, v3, fmt="{}"):
        def f(v):
            return fmt.format(v) if v is not None else "N/A"
        lines.append(f"| {label} | {f(v1)} | {f(v2)} | {f(v3)} |")

    row("usage 확보 건수", f"{c1['requests_with_usage']}/{c1['total_requests']}", f"{c2['requests_with_usage']}/{c2['total_requests']}", f"{c3['requests_with_usage']}/{c3['total_requests']}")
    row("총 prompt 토큰", c1["total_prompt_tokens"], c2["total_prompt_tokens"], c3["total_prompt_tokens"])
    row("총 completion 토큰", c1["total_completion_tokens"], c2["total_completion_tokens"], c3["total_completion_tokens"])
    row("요청당 평균 총 토큰", c1["avg_total_tokens_per_request"], c2["avg_total_tokens_per_request"], c3["avg_total_tokens_per_request"])
    row("요청당 참고 비용(USD)", c1["ref_cost_usd_per_request"], c2["ref_cost_usd_per_request"], c3["ref_cost_usd_per_request"])
    row("요청당 참고 비용(원)", c1["ref_cost_krw_per_request"], c2["ref_cost_krw_per_request"], c3["ref_cost_krw_per_request"])

    lines.append("\n### 노드별 토큰 내역\n")
    all_nodes = sorted(set(c1["node_totals"]) | set(c2["node_totals"]) | set(c3["node_totals"]))
    lines.append(f"| 노드 | {l1} 호출/prompt/completion | {l2} 호출/prompt/completion | {l3} 호출/prompt/completion |")
    lines.append("|---|---|---|---|")
    empty = {"count": 0, "prompt_tokens": 0, "completion_tokens": 0}
    for node in all_nodes:
        a, b, d = c1["node_totals"].get(node, empty), c2["node_totals"].get(node, empty), c3["node_totals"].get(node, empty)
        lines.append(
            f"| {node} | {a['count']}/{a['prompt_tokens']}/{a['completion_tokens']} | "
            f"{b['count']}/{b['prompt_tokens']}/{b['completion_tokens']} | "
            f"{d['count']}/{d['prompt_tokens']}/{d['completion_tokens']} |"
        )

    lines.append("\n## 가격 정확도(추천가 vs 표시된 최저가)\n")
    lines.append(
        "> price_table(여러 판매처 실측가 비교)이 있는 아키텍처에서만 계산 가능하다. "
        "11번가 단일 소스로 동작하는 1st/2nd Proposed는 판매처별 가격 비교 테이블 자체가 "
        "없어(구조적 차이, 버그 아님) 이 지표가 N/A다.\n"
    )
    lines.append(f"| 지표 | {l1} | {l2} | {l3} |")
    lines.append("|---|---|---|---|")
    row("평가 가능 건수", p1["evaluated"], p2["evaluated"], p3["evaluated"])
    row("최저가 정확히 일치", f"{p1['exact_lowest_match']} ({p1['exact_lowest_match_rate']}%)" if p1["evaluated"] else "N/A",
        f"{p2['exact_lowest_match']} ({p2['exact_lowest_match_rate']}%)" if p2["evaluated"] else "N/A",
        f"{p3['exact_lowest_match']} ({p3['exact_lowest_match_rate']}%)" if p3["evaluated"] else "N/A")
    row("평균 최저가 대비 차이(%)", p1["avg_delta_pct"], p2["avg_delta_pct"], p3["avg_delta_pct"])
    row("중앙값 차이(%)", p1["median_delta_pct"], p2["median_delta_pct"], p3["median_delta_pct"])
    row("최대 차이(%)", p1["max_delta_pct"], p2["max_delta_pct"], p3["max_delta_pct"])

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--proposed1", required=True, type=Path)
    parser.add_argument("--proposed2", required=True, type=Path)
    parser.add_argument("--label-baseline", default="Baseline")
    parser.add_argument("--label-proposed1", default="1st Proposed")
    parser.add_argument("--label-proposed2", default="2nd Proposed")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    r1, r2, r3 = load_results(args.baseline), load_results(args.proposed1), load_results(args.proposed2)
    c1, c2, c3 = cost_summary(r1), cost_summary(r2), cost_summary(r3)
    p1, p2, p3 = price_accuracy_summary(r1), price_accuracy_summary(r2), price_accuracy_summary(r3)

    labels = (args.label_baseline, args.label_proposed1, args.label_proposed2)
    report = format_report(labels, (c1, c2, c3), (p1, p2, p3))
    print(report)

    out_path = args.out or (args.baseline.parent / "report_cost_price.md")
    out_path.write_text(report, encoding="utf-8")
    print(f"\n리포트 저장: {out_path}")


if __name__ == "__main__":
    main()
