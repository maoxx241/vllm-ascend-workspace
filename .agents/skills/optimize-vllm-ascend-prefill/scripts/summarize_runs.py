#!/usr/bin/env python3
"""Select the best valid Prefill run under a TTFT SLO."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
from pathlib import Path
from typing import Any


TTFT_KEYS = {
    "average": "ttft_avg_s",
    "avg": "ttft_avg_s",
    "p90": "ttft_p90_s",
    "p99": "ttft_p99_s",
    "max": "ttft_max_s",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True, type=Path)
    parser.add_argument("--ttft-limit-s", required=True, type=float)
    parser.add_argument("--ttft-metric", choices=sorted(TTFT_KEYS), default="p90")
    parser.add_argument("--objective", choices=["qps", "input_token_throughput"], default="qps")
    return parser.parse_args()


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def evaluate(
    run: dict[str, Any], ttft_key: str, limit: float, objective_key: str
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if run.get("case_type") != "prefill":
        reasons.append("case_type is not prefill")
    if run.get("status") != "success":
        reasons.append("status is not success")
    validity = run.get("validity", {})
    if validity.get("valid") is not True:
        reasons.extend(validity.get("reasons") or ["validity.valid is not true"])
    requests = run.get("requests", {})
    total = requests.get("total")
    successful = requests.get("successful")
    failed = requests.get("failed")
    if total is None or successful != total or failed != 0:
        reasons.append("request counts are not all successful")
    output_tokens = finite_number(run.get("workload", {}).get("output_tokens_avg"))
    if output_tokens is None or output_tokens < 1:
        reasons.append("average output tokens is below 1")
    ttft = finite_number(run.get("metrics", {}).get(ttft_key))
    if ttft is None:
        reasons.append(f"missing {ttft_key}")
    elif ttft >= limit:
        reasons.append(f"{ttft_key}={ttft:.6g}s does not satisfy < {limit:.6g}s")
    if finite_number(run.get("metrics", {}).get(objective_key)) is None:
        reasons.append(f"missing or non-finite objective {objective_key}")
    return not reasons, reasons


def flatten(run: dict[str, Any], eligible: bool, reasons: list[str], ttft_key: str) -> dict[str, Any]:
    params = run.get("parameters", {})
    workload = run.get("workload", {})
    requests = run.get("requests", {})
    metrics = run.get("metrics", {})
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "eligible": eligible,
        "reasons": "; ".join(reasons),
        "concurrency": params.get("concurrency"),
        "data_num": params.get("data_num"),
        "max_num_batched_tokens": params.get("max_num_batched_tokens"),
        "long_prefill_token_threshold": params.get("long_prefill_token_threshold"),
        "max_num_seqs": params.get("max_num_seqs"),
        "actual_input_len_avg": workload.get("actual_input_len_avg"),
        "output_tokens_avg": workload.get("output_tokens_avg"),
        "prefix_hit_actual": workload.get("prefix_hit_actual"),
        "successful_requests": requests.get("successful"),
        "failed_requests": requests.get("failed"),
        "actual_concurrency": requests.get("actual_concurrency"),
        "constrained_ttft_s": metrics.get(ttft_key),
        "ttft_avg_s": metrics.get("ttft_avg_s"),
        "ttft_p90_s": metrics.get("ttft_p90_s"),
        "qps": metrics.get("qps"),
        "input_token_throughput": metrics.get("input_token_throughput"),
        "peak_kv_cache_usage": metrics.get("peak_kv_cache_usage"),
    }


def main() -> None:
    args = parse_args()
    session = args.session_dir.resolve()
    summary_dir = session / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    ttft_key = TTFT_KEYS[args.ttft_metric]

    evaluated = []
    for path in sorted((session / "runs").glob("*/result.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        eligible, reasons = evaluate(run, ttft_key, args.ttft_limit_s, args.objective)
        evaluated.append((run, path, eligible, reasons))

    rows = [flatten(run, eligible, reasons, ttft_key) for run, _, eligible, reasons in evaluated]
    csv_path = summary_dir / "experiments.csv"
    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("run_id,status,eligible,reasons\n", encoding="utf-8")

    candidates = []
    for run, path, eligible, _ in evaluated:
        objective = finite_number(run.get("metrics", {}).get(args.objective))
        ttft = finite_number(run.get("metrics", {}).get(ttft_key))
        if eligible and objective is not None and ttft is not None:
            candidates.append((objective, -ttft, run, path))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)

    best_payload: dict[str, Any]
    if candidates:
        _, _, best, best_path = candidates[0]
        best_payload = {
            "selection": {
                "objective": args.objective,
                "ttft_metric": ttft_key,
                "ttft_limit_s": args.ttft_limit_s,
            },
            "source": str(best_path.relative_to(session)),
            "run": best,
        }
        service_script = best.get("artifacts", {}).get("service_script")
        if service_script:
            source = best_path.parent / service_script
            if source.exists():
                destination = session / "best" / "best_service_script.sh"
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    else:
        best_payload = {
            "selection": {
                "objective": args.objective,
                "ttft_metric": ttft_key,
                "ttft_limit_s": args.ttft_limit_s,
            },
            "source": None,
            "run": None,
        }
    (summary_dir / "best.json").write_text(
        json.dumps(best_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    valid_count = sum(eligible for _, _, eligible, _ in evaluated)
    success_count = sum(run.get("status") == "success" for run, _, _, _ in evaluated)
    failed_count = sum(run.get("status") == "failed" for run, _, _, _ in evaluated)
    other_count = len(evaluated) - success_count - failed_count
    lines = [
        "# Prefill optimization report",
        "",
        f"- TTFT constraint: `{ttft_key} < {args.ttft_limit_s:g}s`",
        f"- Objective: `{args.objective}`",
        f"- Experiments: {len(evaluated)}",
        f"- Status success: {success_count}",
        f"- Status failed: {failed_count}",
        f"- Status other/invalid: {other_count}",
        f"- Eligible: {valid_count}",
        f"- Ineligible: {len(evaluated) - valid_count}",
        "",
    ]
    if candidates:
        best = candidates[0][2]
        lines.extend(
            [
                "## Best run",
                "",
                f"- Run: `{best.get('run_id')}`",
                f"- Parameters: `{json.dumps(best.get('parameters', {}), ensure_ascii=False)}`",
                f"- Metrics: `{json.dumps(best.get('metrics', {}), ensure_ascii=False)}`",
                f"- Workload: `{json.dumps(best.get('workload', {}), ensure_ascii=False)}`",
                "",
            ]
        )
    else:
        lines.extend(["## Best run", "", "No run satisfied all validity and TTFT requirements.", ""])
    lines.extend(["## All experiments", "", "See `experiments.csv` for the flattened table.", ""])
    (summary_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "ok" if candidates else "inconclusive",
                "experiments": len(evaluated),
                "eligible": valid_count,
                "best_source": best_payload["source"],
                "summary_dir": str(summary_dir),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
