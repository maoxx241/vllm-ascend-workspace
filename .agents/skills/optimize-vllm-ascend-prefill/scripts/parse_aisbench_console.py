#!/usr/bin/env python3
"""Extract common AISBench metrics from a saved console log."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--console", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def find_summary_dict(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            continue
        try:
            value = ast.literal_eval(stripped)
        except (SyntaxError, ValueError):
            continue
        if isinstance(value, dict) and any(
            key in value for key in ("TTFT avg", "Average TTFT (ms)", "TTFT P90")
        ):
            return value
    raise ValueError("AISBench summary dictionary was not found")


def table_integer(text: str, label: str) -> int | None:
    # A prefix-cache console can contain both warmup and measured summaries.
    # Search backwards so request counts come from the same final phase as the
    # summary dictionary selected by find_summary_dict().
    for line in reversed(text.splitlines()):
        if label not in line:
            continue
        if "|" in line:
            cells = [cell.strip() for cell in line.split("|") if cell.strip()]
            for index, cell in enumerate(cells):
                if label in cell:
                    for candidate in cells[index + 1 :]:
                        match = re.fullmatch(r"[+-]?\d+(?:\.0+)?", candidate.replace(",", ""))
                        if match:
                            return int(float(candidate.replace(",", "")))
        # Fallback for non-table output: take the first integer after the label,
        # not the final integer (which can be a footnote or sample count).
        suffix = line.split(label, 1)[1]
        match = re.search(r"(?<![.\d])[+-]?\d+(?![.\d])", suffix)
        if match:
            return int(match.group())
    return None


def first(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return default


def ms_to_s(value: Any) -> float | None:
    if value is None:
        return None
    return float(value) / 1000.0


def main() -> None:
    args = parse_args()
    text = ANSI_RE.sub("", args.console.read_text(encoding="utf-8", errors="replace"))
    raw = find_summary_dict(text)
    total_value = first(raw, "total_req", "Total requests", "Total Requests", default=0)
    total = int(float(total_value))
    failed = table_integer(text, "Failed Requests")
    successful = table_integer(text, "Success Requests")
    if failed is None:
        failed_value = first(raw, "failed_req", "Failed requests", "Failed Requests")
        failed = int(float(failed_value)) if failed_value is not None else None
    if successful is None:
        successful_value = first(
            raw, "success_req", "Successful requests", "Successful Requests"
        )
        successful = int(float(successful_value)) if successful_value is not None else None
    if failed is None and successful is not None:
        failed = total - successful
    if successful is None and failed is not None:
        successful = total - failed

    result = {
        "requests": {
            "total": total,
            "successful": successful,
            "failed": failed,
            "actual_concurrency": first(raw, "cc", "Actual concurrency"),
            "max_concurrency": first(raw, "max_cc", "Max concurrency"),
        },
        "workload": {
            "actual_input_len_avg": first(raw, "input_len", "Input tokens avg"),
            "output_tokens_avg": first(raw, "output_len", "Output tokens avg"),
        },
        "metrics": {
            "ttft_avg_s": ms_to_s(first(raw, "TTFT avg", "Average TTFT (ms)")),
            "ttft_p90_s": ms_to_s(first(raw, "TTFT P90", "P90 TTFT (ms)")),
            "e2el_avg_s": ms_to_s(first(raw, "E2EL avg", "Average latency (ms)")),
            "e2el_p90_s": ms_to_s(first(raw, "E2EL P90", "P90 latency (ms)")),
            "qps": first(raw, "qps", "Request throughput (req/s)"),
            "input_token_throughput": first(
                raw, "input_token_throughput", "Input token throughput (tok/s)"
            ),
            "output_token_throughput": first(
                raw, "output_throughput", "Output token throughput (tok/s)"
            ),
            "total_token_throughput": first(
                raw, "E2E_throughput", "Total Token throughput (tok/s)"
            ),
            "benchmark_duration_s": first(raw, "E2E_time", "Benchmark Duration"),
        },
        "raw_summary": raw,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(args.output.resolve()),
                "requests": result["requests"],
                "metrics": result["metrics"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
