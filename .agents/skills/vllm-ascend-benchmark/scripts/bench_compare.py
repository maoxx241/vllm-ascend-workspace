#!/usr/bin/env python3
"""Compare vllm bench serve across multiple source states (baseline vs PR).

This is the native, generic multi-state benchmark that previously forced
callers to hand-write a bespoke script (e.g. for DSV4 PR validation). Each
"state" is a git ref checked out *in the container* for vllm-ascend (and
optionally vllm), then benchmarked with an identical serve/bench configuration
so deltas are attributable to code only.

Key properties:
  * Source-only version alignment (``remote_align_source``) -- checks out the
    ref, never rebuilds custom ops. Use parity/serve if a rebuild is needed.
  * Identical serve + bench args across all states (pass once, reused).
  * Optional SAFE stale-process cleanup between states (``--stale-cleanup``)
    that can never kill the session sshd (see ``safe_stale_cleanup``).
  * Warm multi-run per state with warmup discard, aggregated mean/stddev.

Usage:

    # Baseline commit vs a PR, single BS 1x1 512/512, 6 runs (1 warmup)
    python3 bench_compare.py --model /home/weights/DeepSeek-V4-Flash-w4a8-mtp \\
        --state baseline=967c5c3b --state pr10741=pr:10741 \\
        --vllm-ref 967c5c3bc38891f4465d3f4e99917ed837bb3833 \\
        --tp 8 --runs 6 --warmup-runs 1 --stale-cleanup \\
        --serve-args --enable-expert-parallel --quantization ascend \\
        --bench-args --dataset-name random --random-input-len 512 \\
            --random-output-len 512 --num-prompts 1 --max-concurrency 1 --ignore-eos

Progress on stderr as __VAWS_BENCHMARK_PROGRESS__=<json>.
Final comparison on stdout as a single JSON object.
"""

from __future__ import annotations

import argparse
import re
import sys
import traceback
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import (  # noqa: E402
    assemble_config,
    call_serve_start,
    call_serve_stop,
    emit_progress,
    extract_metrics,
    now_utc,
    print_json,
    remote_align_source,
    run_bench_on_remote,
    safe_stale_cleanup,
    safe_token,
    write_local_result,
    _get_ssh_endpoint,
)


def _parse_state(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            "--state must be LABEL=REF, e.g. baseline=967c5c3b or pr10741=pr:10741"
        )
    label, ref = value.split("=", 1)
    label = safe_token(label.strip())
    ref = ref.strip()
    if not label or not ref:
        raise argparse.ArgumentTypeError("--state label/ref cannot be empty")
    return label, ref


def _split_sections(argv: list[str]) -> tuple[list[str], list[str] | None, list[str] | None]:
    delimiters = {"--serve-args", "--bench-args"}
    sections: dict[str, list[str]] = {}
    main_args: list[str] = []
    current: str | None = None
    for token in argv:
        if token in delimiters:
            current = token
            sections[current] = []
        elif current is not None:
            sections[current].append(token)
        else:
            main_args.append(token)
    return main_args, sections.get("--serve-args"), sections.get("--bench-args")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session-id", help="VAWS session id; defaults to the bound session")
    p.add_argument("--session-file", help="explicit session.json path")
    p.add_argument("--model", required=True, help="remote model weight path")
    p.add_argument("--state", action="append", type=_parse_state, required=True,
                   help="LABEL=REF to benchmark (repeatable). REF supports pr:NNNN / commit / branch.")
    p.add_argument("--vllm-ref", help="optional vllm commit/branch to align in-container for every state")
    p.add_argument("--tp", "--tensor-parallel-size", type=int, default=None)
    p.add_argument("--dp", "--data-parallel-size", type=int, default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--extra-env", action="append", default=None,
                   help="KEY=VALUE env vars for the service (repeatable)")
    p.add_argument("--runs", type=int, default=3, help="benchmark iterations per state (default 3)")
    p.add_argument("--warmup-runs", type=int, default=1,
                   help="initial runs discarded from aggregation per state (default 1)")
    p.add_argument("--stale-cleanup", action="store_true",
                   help="run SAFE vLLM stale-process cleanup before/after each state (never kills sshd)")
    p.add_argument("--skip-parity", action="store_true", default=True,
                   help="skip parity sync before serve (default true; states align source directly)")
    return p


def _aggregate(metrics: list[dict[str, Any]], warmup: int) -> dict[str, Any]:
    stat = metrics[warmup:]
    if not stat:
        return {}
    keys: set[str] = set()
    for m in stat:
        keys.update(m.keys())
    agg: dict[str, Any] = {"count": len(stat)}
    for key in sorted(keys):
        vals = []
        for m in stat:
            v = m.get(key)
            if isinstance(v, (int, float)):
                vals.append(float(v))
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        stddev = (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 if len(vals) > 1 else 0.0
        agg[key] = {"mean": round(mean, 4), "stddev": round(stddev, 4), "values": vals}
    return agg


def _run_one_state(
    args: argparse.Namespace,
    label: str,
    ref: str,
    serve_args: list[str] | None,
    bench_args: list[str] | None,
    container_ip: str,
    container_port: int,
    warmup: int,
) -> dict[str, Any]:
    emit_progress("state", f"[{label}] aligning source to {ref}")
    align = remote_align_source(
        container_ip, container_port,
        vllm_ascend_ref=ref,
        vllm_ref=args.vllm_ref,
    )
    if align.get("status") != "ok":
        raise RuntimeError(f"[{label}] source alignment failed: {align}")

    if args.stale_cleanup:
        pre = safe_stale_cleanup(container_ip, container_port)
        emit_progress("cleanup", f"[{label}] pre-clean matched={pre.get('matched_pids')}")

    config = assemble_config(
        session_id=args.session_id,
        session_file=args.session_file,
        model=args.model,
        tp=args.tp,
        dp=args.dp,
        port=args.port,
        serve_args=serve_args,
        bench_args=bench_args,
        extra_env=args.extra_env,
        skip_parity=args.skip_parity,
    )
    try:
        emit_progress("serve", f"[{label}] starting service")
        start = call_serve_start(config)
        if start.get("status") != "ready":
            raise RuntimeError(f"[{label}] service not ready: {str(start)[:1500]}")
        base_url = start["base_url"]
        served_model = start.get("served_model_name", Path(args.model).name)

        metrics: list[dict[str, Any]] = []
        raws: list[dict[str, Any]] = []
        for i in range(max(1, args.runs)):
            is_warm = i < warmup
            emit_progress("bench", f"[{label}] run {i + 1}/{args.runs}{' (warmup)' if is_warm else ''}")
            raw = run_bench_on_remote(config, base_url, served_model, container_ip, container_port)
            metrics.append(extract_metrics(raw))
            raws.append(raw)
    finally:
        call_serve_stop(config, force=True)
        if args.stale_cleanup:
            post = safe_stale_cleanup(container_ip, container_port)
            emit_progress("cleanup", f"[{label}] post-clean matched={post.get('matched_pids')}")

    return {
        "label": label,
        "ref": ref,
        "aligned_heads": align.get("heads", {}),
        "runs": args.runs,
        "warmup_runs": warmup,
        "per_run": [
            {"run": j + 1, "warmup": j < warmup, "metrics": m}
            for j, m in enumerate(metrics)
        ],
        "aggregated": _aggregate(metrics, warmup),
        "raw_results": raws,
    }


def _compare(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not states:
        return []
    base = states[0].get("aggregated", {})
    base_tpot = base.get("mean_tpot_ms", {}).get("mean")
    rows: list[dict[str, Any]] = []
    for st in states:
        agg = st.get("aggregated", {})
        tpot = agg.get("mean_tpot_ms", {}).get("mean")
        row: dict[str, Any] = {
            "label": st["label"],
            "ref": st["ref"],
            "mean_tpot_ms": tpot,
            "std_tpot_ms": agg.get("mean_tpot_ms", {}).get("stddev"),
            "output_throughput": agg.get("output_throughput", {}).get("mean"),
            "spec_decode_acceptance_rate": agg.get("spec_decode_acceptance_rate", {}).get("mean"),
        }
        if isinstance(base_tpot, (int, float)) and isinstance(tpot, (int, float)) and base_tpot:
            row["delta_tpot_ms_vs_first"] = round(tpot - base_tpot, 4)
            row["delta_tpot_pct_vs_first"] = round((tpot - base_tpot) / base_tpot * 100, 3)
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    raw_argv = argv if argv is not None else sys.argv[1:]
    main_argv, serve_args, bench_args = _split_sections(raw_argv)
    args = build_parser().parse_args(main_argv)
    warmup = max(0, min(args.warmup_runs, max(1, args.runs) - 1))

    try:
        # Resolve container endpoint once (session bound / explicit).
        container_ip, container_port = _get_ssh_endpoint(
            session_id=args.session_id,
            session_file=args.session_file,
        )
        states: list[dict[str, Any]] = []
        for label, ref in args.state:
            states.append(
                _run_one_state(
                    args, label, ref, serve_args, bench_args,
                    container_ip, container_port, warmup,
                )
            )
        result = {
            "status": "ok",
            "session_id": args.session_id,
            "model": args.model,
            "vllm_ref": args.vllm_ref,
            "states": [s["label"] for s in states],
            "comparison": _compare(states),
            "state_results": states,
            "serve_args": serve_args,
            "bench_args": bench_args,
            "timestamp": now_utc(),
        }
        # Persist under the session benchmark runs dir (reuse single-run writer).
        from _common import BenchConfig  # local import to avoid cycle noise
        write_local_result(
            BenchConfig(session_id=args.session_id, session_file=args.session_file, model=args.model),
            result,
        )
        print_json(result)
        return 0
    except Exception as e:
        print_json({
            "status": "failed",
            "phase": "compare",
            "error": str(e),
            "traceback": traceback.format_exc(),
        })
        return 2


if __name__ == "__main__":
    sys.exit(main())
