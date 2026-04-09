#!/usr/bin/env python3
"""Run a single vllm bench serve benchmark on a workspace-managed remote container.

Usage examples:

    # Minimal: model + machine
    python3 bench_run.py --machine 173.131.1.2 --model /home/weights/Qwen3.5-35B

    # With explicit serve and bench args
    python3 bench_run.py --machine 173.131.1.2 --model /home/weights/Qwen3.5-35B \\
        --tp 4 --serve-args --async-scheduling --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}' \\
        --bench-args --num-prompts 128 --max-concurrency 32 --output-len 1500

    # Using a nightly config as reference
    python3 bench_run.py --machine 173.131.1.2 --model /home/weights/Qwen3.5-35B \\
        --refer-nightly Qwen3-Next-80B-A3B-Instruct-A2

Progress on stderr as __VAWS_BENCHMARK_PROGRESS__=<json>.
Final result on stdout as a single JSON object.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import (
    assemble_config,
    call_serve_start,
    call_serve_stop,
    emit_progress,
    extract_metrics,
    now_utc,
    print_json,
    run_bench_on_remote,
    _get_ssh_endpoint,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run a single vllm bench serve benchmark.",
        allow_abbrev=False,
    )
    p.add_argument("--machine", required=True, help="machine alias or IP")
    p.add_argument("--model", required=True, help="remote model weight path")
    p.add_argument("--tp", "--tensor-parallel-size", type=int, default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--extra-env", action="append", default=None,
                   help="KEY=VALUE env vars for the service (repeatable)")
    p.add_argument("--refer-nightly", default=None,
                   help="nightly YAML name as configuration reference")
    p.add_argument("--skip-parity", action="store_true")
    return p


def _split_sections(argv: list[str]) -> tuple[list[str], list[str] | None, list[str] | None]:
    """Split argv into (main_args, serve_args, bench_args).

    Recognizes --serve-args and --bench-args as section delimiters in any order.
    """
    delimiters = {"--serve-args", "--bench-args"}
    sections: dict[str, list[str]] = {}
    main_args: list[str] = []
    current_key: str | None = None

    for token in argv:
        if token in delimiters:
            current_key = token
            sections[current_key] = []
        elif current_key is not None:
            sections[current_key].append(token)
        else:
            main_args.append(token)

    return (
        main_args,
        sections.get("--serve-args"),
        sections.get("--bench-args"),
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = argv if argv is not None else sys.argv[1:]
    main_argv, manual_serve_args, manual_bench_args = _split_sections(raw_argv)

    args = build_parser().parse_args(main_argv)

    serve_args = manual_serve_args if manual_serve_args is not None else args.serve_args
    bench_args = manual_bench_args if manual_bench_args is not None else args.bench_args

    try:
        config = assemble_config(
            machine=args.machine,
            model=args.model,
            tp=args.tp,
            port=args.port,
            serve_args=serve_args,
            bench_args=bench_args,
            extra_env=args.extra_env,
            refer_nightly=args.refer_nightly,
            skip_parity=args.skip_parity,
        )

        # --- Start service ---
        emit_progress("start", "launching vllm service")
        start_result = call_serve_start(config)

        if start_result.get("status") != "ready":
            print_json({
                "status": "failed",
                "phase": "serve_start",
                "error": start_result.get("error", "service did not become ready"),
                "serve_result": start_result,
            })
            return 1

        base_url = start_result["base_url"]
        served_model = start_result.get("served_model_name", Path(args.model).name)
        container_ip, container_port = _get_ssh_endpoint(args.machine)

        # --- Run benchmark ---
        emit_progress("bench", "running vllm bench serve")
        try:
            raw_result = run_bench_on_remote(
                config, base_url, served_model, container_ip, container_port,
            )
        except Exception as e:
            emit_progress("bench", f"benchmark failed: {e}")
            call_serve_stop(args.machine, force=True)
            print_json({
                "status": "failed",
                "phase": "bench_run",
                "error": str(e),
                "config": config.summary_dict(),
            })
            return 1

        # --- Stop service ---
        emit_progress("stop", "stopping service")
        call_serve_stop(args.machine)

        # --- Output ---
        metrics = extract_metrics(raw_result)
        emit_progress("done", f"benchmark complete, throughput={metrics.get('output_throughput', 'N/A')}")

        print_json({
            "status": "ok",
            "machine": args.machine,
            "model": args.model,
            "metrics": metrics,
            "config": config.summary_dict(),
            "raw_result": raw_result,
            "timestamp": now_utc(),
        })
        return 0

    except Exception as e:
        call_serve_stop(args.machine, force=True)
        print_json({
            "status": "failed",
            "phase": "unexpected",
            "error": str(e),
            "traceback": traceback.format_exc(),
        })
        return 2


if __name__ == "__main__":
    sys.exit(main())
