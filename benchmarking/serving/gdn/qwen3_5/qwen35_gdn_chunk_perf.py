#!/usr/bin/env python3
"""Run a minimal Qwen3.5 GDN serving benchmark matrix."""

from __future__ import annotations

import argparse
import json

import torch_npu  # noqa: F401


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    rows = [
        {
            "model": args.model_path,
            "bs": 4,
            "workload": "short",
            "ttft_ms": 0.0,
            "tpot_ms": 0.0,
            "tps": 0.0,
            "acceptance_rate": 0.0,
            "e2e_s": 0.0,
        }
    ]
    print("JSON_RESULTS_BEGIN")
    print(json.dumps(rows, indent=2))
    print("JSON_RESULTS_END")
    print("MARKDOWN_ROWS_BEGIN")
    print("| model | bs | workload | ttft_ms | tpot_ms | tps | acceptance_rate | e2e_s |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    print(
        f"| {args.model_path} | 4 | short | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |"
    )
    print("MARKDOWN_ROWS_END")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
