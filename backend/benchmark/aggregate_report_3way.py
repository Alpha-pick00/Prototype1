"""baseline vs 1st proposed vs 2nd proposed 3웨이 벤치마크 결과 집계 리포트.
표준 라이브러리만 사용(pandas 없음). aggregate_report.py의 2웨이 로직을 재사용해
세 번째 축을 추가한 변형."""

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


def precision_recall_f1(results: list[dict]) -> dict:
    """단일 결정(top-1) 파이프라인을 이진 분류로 재정의해 계산한다 - 모든 쿼리에
    실제 관련 상품이 존재한다고 가정(포지티브 전수)하므로 TN은 없다.

    TP: 추천 성공(status=ok) + 관련성 검증 통과(relevance_match=True)
    FP: 추천 성공 + 관련성 검증 실패(relevance_match=False, 즉 무관한 상품 채택)
    FN: 추천 실패(status!=ok, 5xx/타임아웃 등) 또는 관련성 채점 자체가 안 됨
        (product_name 누락) - "존재했을 정답을 못 찾음"으로 취급

    이 정의에서 Recall은 사실상 "성공률 중 관련성까지 맞춘 비율"과 동치이고,
    Precision은 "추천을 냈을 때 그게 맞을 확률"이다. MAP/nDCG류 랭킹 지표는
    top-1 단일 결정 구조(후보 순위 리스트를 저장하지 않음)라 정의 자체가
    성립하지 않아 포함하지 않는다."""
    tp = fp = fn = 0
    for r in results:
        if r["status"] == "ok" and r.get("relevance_match") is not None:
            if r["relevance_match"]:
                tp += 1
            else:
                fp += 1
        else:
            fn += 1
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)) if (precision and recall and (precision + recall) > 0) else None
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision * 100, 1) if precision is not None else None,
        "recall": round(recall * 100, 1) if recall is not None else None,
        "f1": round(f1 * 100, 1) if f1 is not None else None,
    }


def classify_failure(r: dict) -> str:
    """실패 원인을 대략적인 유형으로 분류한다 - 벤치마크 로그 텍스트 기반 휴리스틱이라
    완벽하지 않지만, "진짜 아키텍처 문제"와 "인프라/쿼터 문제"를 구분하는 데는
    충분하다. 라이브 API를 쓰는 이 벤치마크는 프로바이더 쿼터 소진처럼
    아키텍처와 무관한 실패가 섞여 들어오기 쉬워, 이 구분이 없으면 실패율이
    실제 성능과 다른 것을 측정하게 된다."""
    err = (r.get("error") or "").lower()
    if r.get("status") == "ok":
        return "ok"
    if "timeout" in err or "readtimeout" in err:
        return "timeout"
    if "http 5" in err or "502" in err or "503" in err or "504" in err:
        return "5xx"
    if "quota" in err or "rate limit" in err or "429" in err or "432" in err:
        return "provider_quota"
    if "connect" in err or "connection" in err:
        return "connection"
    return "other"


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    latencies = [r["latency_ms"] for r in ok if r["latency_ms"] is not None]
    matches = [r["relevance_match"] for r in ok if r["relevance_match"] is not None]
    prf = precision_recall_f1(results)

    failure_types: dict[str, int] = defaultdict(int)
    for r in failed:
        failure_types[classify_failure(r)] += 1

    node_totals: dict[str, dict[str, int]] = defaultdict(lambda: {"prompt": 0, "completion": 0, "count": 0})
    total_prompt = 0
    total_completion = 0
    for r in ok:
        # usage_by_node(2026-08-31, X-Usage 헤더 기반)가 새 필드고, usage(옛
        # usage_log.jsonl 방식, 이제 어느 워크트리에도 그 로그 파일이 없어
        # 항상 빈 리스트)는 하위호환으로만 남긴다.
        for node, bucket in (r.get("usage_by_node") or {}).items():
            p = bucket.get("prompt_tokens") or 0
            c = bucket.get("completion_tokens") or 0
            node_totals[node]["prompt"] += p
            node_totals[node]["completion"] += c
            node_totals[node]["count"] += 1
            total_prompt += p
            total_completion += c
        for u in r.get("usage") or []:
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
        "precision": prf["precision"],
        "recall": prf["recall"],
        "f1": prf["f1"],
        "tp": prf["tp"],
        "fp": prf["fp"],
        "fn": prf["fn"],
        "failure_types": dict(failure_types),
    }


def summarize_by_category(results: list[dict]) -> dict[str, dict]:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    return {cat: summarize(rows) for cat, rows in sorted(by_cat.items())}


def category_variance(by_cat: dict[str, dict]) -> dict:
    """카테고리별 관련성 매칭률의 산포도 - 평균만으로는 "전 카테고리에 고르게
    괜찮은지" vs "몇 카테고리가 유독 나쁜데 나머지가 끌어올린 평균인지"를 구분할
    수 없어, 표준편차와 최솟값/최댓값을 함께 본다."""
    rates = [c["relevance_match_rate"] for c in by_cat.values() if c.get("relevance_match_rate") is not None]
    if not rates:
        return {"stdev": None, "min": None, "min_cat": None, "max": None, "max_cat": None}
    stdev = round(statistics.pstdev(rates), 1) if len(rates) > 1 else 0.0
    min_cat = min(by_cat.items(), key=lambda kv: kv[1].get("relevance_match_rate") if kv[1].get("relevance_match_rate") is not None else 999)
    max_cat = max(by_cat.items(), key=lambda kv: kv[1].get("relevance_match_rate") if kv[1].get("relevance_match_rate") is not None else -1)
    return {
        "stdev": stdev,
        "min": min_cat[1].get("relevance_match_rate"),
        "min_cat": min_cat[0],
        "max": max_cat[1].get("relevance_match_rate"),
        "max_cat": max_cat[0],
    }


def format_report(
    labels: tuple[str, str, str],
    summaries: tuple[dict, dict, dict],
    by_cats: tuple[dict, dict, dict],
) -> str:
    l1, l2, l3 = labels
    s1, s2, s3 = summaries
    c1, c2, c3 = by_cats
    lines = []
    lines.append(f"# {l1} vs {l2} vs {l3} 벤치마크 리포트\n")

    lines.append("## 전체 요약\n")
    lines.append(f"| 지표 | {l1} | {l2} | {l3} |")
    lines.append("|---|---|---|---|")

    def row(label, v1, v2, v3, fmt="{}"):
        def fmt_val(v):
            return fmt.format(v) if v is not None else "N/A"
        lines.append(f"| {label} | {fmt_val(v1)} | {fmt_val(v2)} | {fmt_val(v3)} |")

    row("요청 수 (성공/전체)", f"{s1['ok']}/{s1['total']}", f"{s2['ok']}/{s2['total']}", f"{s3['ok']}/{s3['total']}")
    row("평균 레이턴시 (ms)", s1["avg_latency_ms"], s2["avg_latency_ms"], s3["avg_latency_ms"])
    row("p50 레이턴시 (ms)", s1["p50_latency_ms"], s2["p50_latency_ms"], s3["p50_latency_ms"])
    row("p95 레이턴시 (ms)", s1["p95_latency_ms"], s2["p95_latency_ms"], s3["p95_latency_ms"])
    row("총 prompt 토큰", s1["total_prompt_tokens"], s2["total_prompt_tokens"], s3["total_prompt_tokens"])
    row("총 completion 토큰", s1["total_completion_tokens"], s2["total_completion_tokens"], s3["total_completion_tokens"])
    row("요청당 평균 총 토큰", s1["avg_total_tokens_per_request"], s2["avg_total_tokens_per_request"], s3["avg_total_tokens_per_request"])
    row("관련성 매칭률 (%)", s1["relevance_match_rate"], s2["relevance_match_rate"], s3["relevance_match_rate"])
    row("Precision (%)", s1["precision"], s2["precision"], s3["precision"])
    row("Recall (%)", s1["recall"], s2["recall"], s3["recall"])
    row("F1 (%)", s1["f1"], s2["f1"], s3["f1"])
    row("TP / FP / FN", f"{s1['tp']}/{s1['fp']}/{s1['fn']}", f"{s2['tp']}/{s2['fp']}/{s2['fn']}", f"{s3['tp']}/{s3['fp']}/{s3['fn']}")

    lines.append("\n> 관련성 매칭률은 `_product_name_matches`(rapidfuzz 기반)로 자동 채점한 구조적")
    lines.append("> 관련성이다 - 표기가 다른(단위 \"기가\" vs \"GB\" 등) 실제 정답도 임계값 미달로")
    lines.append("> False가 나올 수 있어 절대 수치보다 세 버전 간 상대적 차이에 무게를 둬야 한다.\n")
    lines.append("> Precision/Recall/F1은 이 파이프라인이 쿼리당 최종 추천 1개만 내는 단일 결정")
    lines.append("> 구조라는 점을 반영해 이진 분류로 재정의했다: TP=추천 성공+관련성 통과,")
    lines.append("> FP=추천은 냈지만 무관한 상품, FN=추천 자체에 실패(5xx/타임아웃 등, 모든")
    lines.append("> 쿼리에 정답이 존재한다고 가정). Recall은 \"전체 쿼리 중 관련 상품을 찾아낸")
    lines.append("> 비율\", Precision은 \"추천했을 때 그게 맞을 확률\"이다. top-1 단일 결정이라")
    lines.append("> 후보 순위 리스트가 없어 MAP/nDCG 등 랭킹 지표는 정의 자체가 성립하지 않는다.\n")

    lines.append("## 노드별 토큰 세부\n")
    lines.append(f"| 노드 | {l1} 호출수 | {l1} prompt | {l1} completion | {l2} 호출수 | {l2} prompt | {l2} completion | {l3} 호출수 | {l3} prompt | {l3} completion |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    all_nodes = sorted(set(s1["node_totals"]) | set(s2["node_totals"]) | set(s3["node_totals"]))
    empty = {"count": 0, "prompt": 0, "completion": 0}
    for node in all_nodes:
        n1 = s1["node_totals"].get(node, empty)
        n2 = s2["node_totals"].get(node, empty)
        n3 = s3["node_totals"].get(node, empty)
        lines.append(
            f"| {node} | {n1['count']} | {n1['prompt']} | {n1['completion']} | "
            f"{n2['count']} | {n2['prompt']} | {n2['completion']} | "
            f"{n3['count']} | {n3['prompt']} | {n3['completion']} |"
        )

    lines.append("\n## 카테고리별 세부\n")
    lines.append(f"| 카테고리 | {l1} 매칭률(%) | {l2} 매칭률(%) | {l3} 매칭률(%) | {l1} 평균지연(ms) | {l2} 평균지연(ms) | {l3} 평균지연(ms) |")
    lines.append("|---|---|---|---|---|---|---|")
    all_cats = sorted(set(c1) | set(c2) | set(c3))
    for cat in all_cats:
        a = c1.get(cat, {})
        b = c2.get(cat, {})
        d = c3.get(cat, {})
        lines.append(
            f"| {cat} | {a.get('relevance_match_rate', 'N/A')} | {b.get('relevance_match_rate', 'N/A')} | {d.get('relevance_match_rate', 'N/A')} | "
            f"{a.get('avg_latency_ms', 'N/A')} | {b.get('avg_latency_ms', 'N/A')} | {d.get('avg_latency_ms', 'N/A')} |"
        )

    lines.append(f"\n| 카테고리 | {l1} F1(%) | {l2} F1(%) | {l3} F1(%) | {l1} Precision(%) | {l2} Precision(%) | {l3} Precision(%) | {l1} Recall(%) | {l2} Recall(%) | {l3} Recall(%) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for cat in all_cats:
        a = c1.get(cat, {})
        b = c2.get(cat, {})
        d = c3.get(cat, {})
        lines.append(
            f"| {cat} | {a.get('f1', 'N/A')} | {b.get('f1', 'N/A')} | {d.get('f1', 'N/A')} | "
            f"{a.get('precision', 'N/A')} | {b.get('precision', 'N/A')} | {d.get('precision', 'N/A')} | "
            f"{a.get('recall', 'N/A')} | {b.get('recall', 'N/A')} | {d.get('recall', 'N/A')} |"
        )

    lines.append("\n## 실패 건수\n")
    lines.append(f"- {l1}: {s1['failed']}건 실패 / {s1['total']}건")
    lines.append(f"- {l2}: {s2['failed']}건 실패 / {s2['total']}건")
    lines.append(f"- {l3}: {s3['failed']}건 실패 / {s3['total']}건")

    lines.append("\n## 실패 유형 분류\n")
    lines.append(f"| 유형 | {l1} | {l2} | {l3} |")
    lines.append("|---|---|---|---|")
    all_types = sorted(set(s1["failure_types"]) | set(s2["failure_types"]) | set(s3["failure_types"]))
    type_labels = {
        "timeout": "타임아웃",
        "5xx": "5xx (서버/파이프라인 오류)",
        "provider_quota": "프로바이더 쿼터/레이트리밋",
        "connection": "연결 실패",
        "other": "기타",
    }
    if all_types:
        for t in all_types:
            label = type_labels.get(t, t)
            lines.append(f"| {label} | {s1['failure_types'].get(t, 0)} | {s2['failure_types'].get(t, 0)} | {s3['failure_types'].get(t, 0)} |")
    else:
        lines.append("| (실패 없음) | 0 | 0 | 0 |")
    lines.append("\n> 실패 원인을 에러 메시지 텍스트로 대략 분류한 것이다. \"프로바이더")
    lines.append("> 쿼터/레이트리밋\"은 아키텍처 자체의 결함이 아니라 라이브 벤치마크가 무료")
    lines.append("> API 티어에 의존할 때 생기는 인프라성 실패이므로, 이 항목이 큰 버전은 실제")
    lines.append("> 실패율보다 아키텍처 실패율(5xx/기타만)이 더 낮을 수 있다는 점을 감안해야")
    lines.append("> 한다.\n")

    lines.append("## 카테고리별 편차(관련성 매칭률)\n")
    v1, v2, v3 = category_variance(c1), category_variance(c2), category_variance(c3)
    lines.append(f"| | {l1} | {l2} | {l3} |")
    lines.append("|---|---|---|---|")
    lines.append(f"| 표준편차 | {v1['stdev']} | {v2['stdev']} | {v3['stdev']} |")
    lines.append(
        f"| 최저 카테고리 | {v1['min_cat']} ({v1['min']}%) | {v2['min_cat']} ({v2['min']}%) | {v3['min_cat']} ({v3['min']}%) |"
    )
    lines.append(
        f"| 최고 카테고리 | {v1['max_cat']} ({v1['max']}%) | {v2['max_cat']} ({v2['max']}%) | {v3['max_cat']} ({v3['max']}%) |"
    )
    lines.append("\n> 표준편차가 작을수록 카테고리 간 일관성이 높다는 뜻이다 - 평균 매칭률이")
    lines.append("> 같아도 편차가 큰 쪽은 특정 카테고리에서 유독 취약하다는 의미이므로, 평균과")
    lines.append("> 함께 봐야 한다.")

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

    r1 = load_results(args.baseline)
    r2 = load_results(args.proposed1)
    r3 = load_results(args.proposed2)

    s1, s2, s3 = summarize(r1), summarize(r2), summarize(r3)
    c1, c2, c3 = summarize_by_category(r1), summarize_by_category(r2), summarize_by_category(r3)

    labels = (args.label_baseline, args.label_proposed1, args.label_proposed2)
    report = format_report(labels, (s1, s2, s3), (c1, c2, c3))
    print(report)

    out_path = args.out or (args.baseline.parent / "report_3way.md")
    out_path.write_text(report, encoding="utf-8")
    print(f"\n리포트 저장: {out_path}")


if __name__ == "__main__":
    main()
