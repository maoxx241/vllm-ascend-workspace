#!/usr/bin/env python3
"""A/B benchmark comparison on a workspace-managed remote container.

Checks out baseline and patched refs in a submodule, runs vllm bench serve
for each, and outputs a comparison JSON with delta percentages.

Usage example:

    python3 bench_compare.py \\
        --machine 173.125.1.2 \\
        --baseline-ref main \\
        --patched-ref feat/my-opt \\
        --repo vllm-ascend \\
        --model /home/weights/Qwen3.5-0.8B \\
        --tp 1 \\
        --patched-extra-env VLLM_LOGGING_LEVEL=DEBUG

Progress on stderr as __VAWS_BENCHMARK_PROGRESS__=<json>.
Final result on stdout as a single JSON object.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import traceback
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import (
    ROOT,
    BenchConfig,
    assemble_config,
    call_serve_start,
    call_serve_stop,
    check_regression,
    compute_delta,
    emit_progress,
    extract_metrics,
    now_utc,
    print_json,
    run_bench_on_remote,
    _get_ssh_endpoint,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="A/B benchmark comparison.",
        allow_abbrev=False,
    )
    p.add_argument("--machine", required=True, help="machine alias or IP")
    p.add_argument("--baseline-ref", required=True, help="baseline branch/commit")
    p.add_argument("--patched-ref", required=True, help="patched branch/commit")
    p.add_argument("--repo", required=True, choices=["vllm-ascend", "vllm"],
                   help="which submodule to switch refs on")
    p.add_argument("--model", required=True, help="remote model weight path")
    p.add_argument("--tp", "--tensor-parallel-size", type=int, default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--extra-env", action="append", default=None,
                   help="KEY=VALUE env for BOTH sides (repeatable)")
    p.add_argument("--patched-extra-env", action="append", default=None,
                   help="KEY=VALUE env for patched side ONLY (repeatable)")
    p.add_argument("--refer-nightly", default=None)
    return p


def _split_sections(argv: list[str]) -> tuple[list[str], list[str] | None, list[str] | None]:
    """Split argv into (main_args, serve_args, bench_args)."""
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


def _checkout_ref(repo: str, ref: str) -> str:
    """Checkout a ref in the given submodule. Returns the resolved commit hash."""
    repo_path = ROOT / repo
    if not repo_path.exists():
        raise RuntimeError(f"submodule directory not found: {repo_path}")

    subprocess.run(
        ["git", "fetch", "--all", "--quiet"],
        cwd=str(repo_path), check=False, capture_output=True,
    )

    result = subprocess.run(
        ["git", "checkout", ref],
        cwd=str(repo_path), check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        result = subprocess.run(
            ["git", "checkout", f"origin/{ref}"],
            cwd=str(repo_path), check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"cannot checkout {ref!r} in {repo}: {result.stderr[:500]}"
            )

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path), capture_output=True, text=True, check=True,
    )
    return commit.stdout.strip()


def _run_single_bench(
    config: BenchConfig,
    label: str,
) -> dict[str, Any]:
    """Run one benchmark cycle: start -> bench -> stop. Returns metrics + raw."""
    from typing import Any

    emit_progress(f"{label}_start", f"[{label}] starting service")
    start_result = call_serve_start(config)
    if start_result.get("status") != "ready":
        raise RuntimeError(
            f"[{label}] service did not become ready: "
            f"{start_result.get('error', str(start_result))}"
        )

    base_url = start_result["base_url"]
    served_model = start_result.get("served_model_name", Path(config.model).name)
    container_ip, container_port = _get_ssh_endpoint(config.machine)

    emit_progress(f"{label}_bench", f"[{label}] running vllm bench serve")
    try:
        raw_result = run_bench_on_remote(
            config, base_url, served_model, container_ip, container_port,
        )
    except Exception:
        call_serve_stop(config.machine, force=True)
        raise

    emit_progress(f"{label}_stop", f"[{label}] stopping service")
    call_serve_stop(config.machine)

    metrics = extract_metrics(raw_result)
    emit_progress(f"{label}_done", f"[{label}] throughput={metrics.get('output_throughput', 'N/A')}")
    return {"metrics": metrics, "raw_result": raw_result}


def main(argv: list[str] | None = None) -> int:
    raw_argv = argv if argv is not None else sys.argv[1:]
    main_argv, manual_serve_args, manual_bench_args = _split_sections(raw_argv)
    args = build_parser().parse_args(main_argv)
    serve_args = manual_serve_args if manual_serve_args is not None else args.serve_args
    bench_args = manual_bench_args if manual_bench_args is not None else args.bench_args

    original_head: str | None = None

    try:
        repo_path = ROOT / args.repo
        original_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path), capture_output=True, text=True, check=True,
        ).stdout.strip()

        base_config = assemble_config(
            machine=args.machine,
            model=args.model,
            tp=args.tp,
            port=args.port,
            serve_args=serve_args,
            bench_args=bench_args,
            extra_env=args.extra_env,
            refer_nightly=args.refer_nightly,
            skip_parity=False,
        )

        # --- Baseline ---
        emit_progress("checkout", f"checking out baseline: {args.baseline_ref}")
        baseline_commit = _checkout_ref(args.repo, args.baseline_ref)
        emit_progress("checkout", f"baseline at {baseline_commit[:12]}")

        baseline_result = _run_single_bench(base_config, "baseline")

        # --- Patched ---
        emit_progress("checkout", f"checking out patched: {args.patched_ref}")
        patched_commit = _checkout_ref(args.repo, args.patched_ref)
        emit_progress("checkout", f"patched at {patched_commit[:12]}")

        patched_config = assemble_config(
            machine=args.machine,
            model=args.model,
            tp=args.tp,
            port=args.port,
            serve_args=serve_args,
            bench_args=bench_args,
            extra_env=args.extra_env,
            refer_nightly=args.refer_nightly,
            skip_parity=False,
        )

        patched_env_additions: dict[str, str] = {}
        if args.patched_extra_env:
            for item in args.patched_extra_env:
                if "=" in item:
                    k, v = item.split("=", 1)
                    patched_config.env[k] = v
                    patched_env_additions[k] = v

        patched_result = _run_single_bench(patched_config, "patched")

        # --- Comparison ---
        baseline_metrics = baseline_result["metrics"]
        patched_metrics = patched_result["metrics"]
        delta = compute_delta(baseline_metrics, patched_metrics)
        regression = check_regression(baseline_metrics, patched_metrics)

        env_diff: dict[str, Any] = {}
        if patched_env_additions:
            env_diff["patched_only"] = [f"{k}={v}" for k, v in patched_env_additions.items()]

        emit_progress("done", f"comparison complete, regression={regression}")

        print_json({
            "status": "ok",
            "machine": args.machine,
            "model": args.model,
            "repo": args.repo,
            "baseline": {
                "ref": args.baseline_ref,
                "commit": baseline_commit,
                "metrics": baseline_metrics,
                "env": base_config.env,
            },
            "patched": {
                "ref": args.patched_ref,
                "commit": patched_commit,
                "metrics": patched_metrics,
                "env": patched_config.env,
            },
            "delta": delta,
            "env_diff": env_diff,
            "regression": regression,
            "config": base_config.summary_dict(),
            "timestamp": now_utc(),
        })
        return 0

    except Exception as e:
        try:
            call_serve_stop(args.machine, force=True)
        except Exception:
            pass
        print_json({
            "status": "failed",
            "error": str(e),
            "traceback": traceback.format_exc(),
        })
        return 2

    finally:
        if original_head:
            subprocess.run(
                ["git", "checkout", original_head],
                cwd=str(ROOT / args.repo),
                check=False, capture_output=True,
            )
            emit_progress("cleanup", f"restored {args.repo} to {original_head[:12]}")


if __name__ == "__main__":
    sys.exit(main())
