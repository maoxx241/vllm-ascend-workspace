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
  * Identical serve + bench args across all states (pass once, reused, or
    pull them from a named ``--preset`` under the skill's ``presets/`` dir).
  * Native-input fingerprint gate: per state the csrc/cmake/requirements
    digest of the in-container checkout is compared against the first state's
    so stale compiled artifacts are caught instead of silently compared
    (``--allow-stale-native`` downgrades the failure to a warning).
  * Optional SAFE stale-process cleanup between states (``--stale-cleanup``)
    that can never kill the session sshd (see ``safe_stale_cleanup``).
  * Warm multi-run per state with warmup discard, aggregated mean/stddev.
  * Every completed state is persisted immediately; on failure the error JSON
    still carries ``partial_states`` and ``result_paths``.

Usage:

    # Baseline commit vs a PR via the DSV4 Flash preset (6 runs, 1 warmup)
    python3 bench_compare.py --preset dsv4-flash \\
        --model /home/weights/DeepSeek-V4-Flash-w4a8-mtp \\
        --state baseline=967c5c3b --state pr10741=pr:10741 \\
        --stale-cleanup --fixed-request-dataset --accuracy-probe

    # Fully explicit configuration (no preset)
    python3 bench_compare.py --model /home/weights/Qwen3.5-35B \\
        --state baseline=main --state pr10741=pr:10741 \\
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
import shlex
import sys
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import (  # noqa: E402
    BenchConfig,
    apply_remote_patch,
    assemble_config,
    call_serve_start,
    call_serve_stop,
    emit_progress,
    extract_metrics,
    load_preset,
    now_utc,
    prepare_fixed_request_dataset,
    print_json,
    remote_align_source,
    remote_native_input_digest,
    run_accuracy_probe,
    run_bench_on_remote,
    safe_stale_cleanup,
    safe_token,
    write_local_result,
    _get_ssh_endpoint,
)

DEFAULT_FIXED_INPUT_LEN = 512
DEFAULT_FIXED_OUTPUT_LEN = 512
DEFAULT_ACCURACY_MAX_TOKENS = 64
DEFAULT_ACCURACY_PROMPT = (
    "Solve this exactly and return only the final integer: "
    "12345 + 67890 - 11111."
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


def _parse_request_counts(value: str) -> list[int]:
    counts: list[int] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            count = int(item)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"--bench-request-counts entry {item!r} is not an integer"
            )
        if count <= 0:
            raise argparse.ArgumentTypeError(
                "--bench-request-counts entries must be positive"
            )
        counts.append(count)
    if not counts:
        raise argparse.ArgumentTypeError("--bench-request-counts must not be empty")
    return counts


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
    p.add_argument("--preset",
                   help="named benchmark preset from the skill's presets/ dir "
                        "(e.g. dsv4-flash); explicit CLI args override preset values")
    p.add_argument("--vllm-ref", help="optional vllm commit/branch to align in-container for every state")
    p.add_argument("--tp", "--tensor-parallel-size", type=int, default=None)
    p.add_argument("--dp", "--data-parallel-size", type=int, default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--served-model-name", default=None,
                   help="served model name for the API (default: preset or model basename)")
    p.add_argument("--devices", default=None,
                   help="ASCEND_RT_VISIBLE_DEVICES, e.g. 0,1,2,3,4,5,6,7")
    p.add_argument("--health-timeout", type=int, default=None,
                   help="service readiness timeout in seconds")
    p.add_argument("--extra-env", action="append", default=None,
                   help="KEY=VALUE env vars for the service (repeatable)")
    p.add_argument("--bench-env", action="append", default=None,
                   help="KEY=VALUE env vars for the bench-side remote shell (repeatable), "
                        "e.g. PYTHONPATH=/vllm-workspace/vllm")
    p.add_argument("--runs", type=int, default=None,
                   help="benchmark iterations per state per case (CLI > preset > 3)")
    p.add_argument("--warmup-runs", type=int, default=None,
                   help="initial runs discarded from aggregation per case (CLI > preset > 1)")
    p.add_argument("--stale-cleanup", action="store_true",
                   help="run SAFE vLLM stale-process cleanup before/after each state (never kills sshd)")
    p.add_argument("--bench-request-counts", type=_parse_request_counts, default=None,
                   help="comma-separated request counts, e.g. 1,2; each count becomes a "
                        "case that overrides --num-prompts and --max-concurrency")
    p.add_argument("--fixed-request-dataset", action="store_true",
                   help="generate one fixed-token-count JSONL dataset on the remote and "
                        "benchmark it via --dataset-name custom (auto-enabled when the "
                        "preset carries fixed_request_dataset)")
    p.add_argument("--fixed-input-len", type=int, default=None,
                   help=f"exact prompt token length for the fixed dataset (default {DEFAULT_FIXED_INPUT_LEN})")
    p.add_argument("--fixed-output-len", type=int, default=None,
                   help=f"output tokens per fixed-dataset request (default {DEFAULT_FIXED_OUTPUT_LEN})")
    p.add_argument("--fixed-dataset-path", default=None,
                   help="remote JSONL path for the generated fixed dataset")
    p.add_argument("--fixed-prompt", default=None,
                   help="explicit fixed prompt text; its tokenized length must equal --fixed-input-len")
    p.add_argument("--accuracy-probe", action="store_true",
                   help="run one deterministic completion after service ready, before benching")
    p.add_argument("--accuracy-prompt", default=DEFAULT_ACCURACY_PROMPT,
                   help="prompt used by --accuracy-probe")
    p.add_argument("--accuracy-max-tokens", type=int, default=DEFAULT_ACCURACY_MAX_TOKENS,
                   help=f"max_tokens for --accuracy-probe (default {DEFAULT_ACCURACY_MAX_TOKENS})")
    p.add_argument("--remote-patch-file", default=None,
                   help="local patch file git-applied to the in-container vllm-ascend "
                        "checkout after each state's source alignment")
    p.add_argument("--allow-stale-native", action="store_true",
                   help="warn instead of fail when csrc/cmake/requirements inputs differ "
                        "between states (compiled artifacts are NOT rebuilt)")
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


def _opt_value(args_list: list[str], opt: str) -> str | None:
    """Return the value of ``--opt`` (``--opt value`` or ``--opt=value``), or None."""
    for i, token in enumerate(args_list):
        if token == opt and i + 1 < len(args_list):
            return args_list[i + 1]
        if token.startswith(f"{opt}="):
            return token.split("=", 1)[1]
    return None


def _set_opt(args_list: list[str], opt: str, value: str) -> list[str]:
    """Set ``--opt value`` in an args list, replacing any existing occurrence."""
    out: list[str] = []
    skip_next = False
    done = False
    for token in args_list:
        if skip_next:
            skip_next = False
            continue
        if token == opt:
            skip_next = True
            if not done:
                out.extend([opt, value])
                done = True
            continue
        if token.startswith(f"{opt}="):
            if not done:
                out.extend([opt, value])
                done = True
            continue
        out.append(token)
    if not done:
        out.extend([opt, value])
    return out


# Value-taking and bare bench flags that select a dataset; stripped before the
# fixed custom-dataset flags are appended so the two never conflict.
_DATASET_VALUE_OPTS = (
    "--dataset-name",
    "--dataset-path",
    "--random-input-len",
    "--random-output-len",
    "--random-range-ratio",
    "--custom-output-len",
    "--custom-input-len",
    "--sonnet-input-len",
    "--sonnet-output-len",
    "--sharegpt-output-len",
)
_DATASET_BARE_OPTS = (
    "--ignore-eos",
    "--disable-shuffle",
    "--skip-chat-template",
)


def fixed_dataset_bench_args(
    base: list[str],
    *,
    dataset_path: str,
    output_len: int,
) -> list[str]:
    """Switch bench args to the generated fixed custom dataset.

    Dataset-selecting flags in ``base`` are stripped, then the custom dataset
    flags are appended: ``--dataset-name custom --dataset-path <path>
    --custom-output-len <n> --skip-chat-template --disable-shuffle
    --ignore-eos``.
    """
    out: list[str] = []
    skip_next = False
    for token in base:
        if skip_next:
            skip_next = False
            continue
        if token in _DATASET_VALUE_OPTS:
            skip_next = True
            continue
        if token in _DATASET_BARE_OPTS:
            continue
        if any(token.startswith(f"{opt}=") for opt in _DATASET_VALUE_OPTS):
            continue
        out.append(token)
    out.extend([
        "--dataset-name", "custom",
        "--dataset-path", dataset_path,
        "--custom-output-len", str(output_len),
        "--skip-chat-template",
        "--disable-shuffle",
        "--ignore-eos",
    ])
    return out


def bench_case_label(request_count: int | None) -> str:
    return "default" if request_count is None else f"requests_{request_count}"


def build_cases(
    base_bench_args: list[str],
    request_counts: list[int | None],
    *,
    fixed: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the per-case bench args for each request count.

    ``fixed`` carries ``{"path": ..., "output_len": ...}`` when the generated
    fixed dataset replaces the configured dataset. A non-None request count
    overrides both ``--num-prompts`` and ``--max-concurrency`` for that case.
    """
    cases: list[dict[str, Any]] = []
    for count in request_counts:
        case_args = list(base_bench_args)
        if fixed is not None:
            case_args = fixed_dataset_bench_args(
                case_args,
                dataset_path=fixed["path"],
                output_len=fixed["output_len"],
            )
        if count is not None:
            case_args = _set_opt(case_args, "--num-prompts", str(count))
            case_args = _set_opt(case_args, "--max-concurrency", str(count))
        cases.append({
            "case": bench_case_label(count),
            "request_count": count,
            "bench_args": case_args,
        })
    return cases


def _run_one_state(
    args: argparse.Namespace,
    config: BenchConfig,
    label: str,
    ref: str,
    *,
    vllm_ref: str | None,
    cases: list[dict[str, Any]],
    container_ip: str,
    container_port: int,
    runs: int,
    warmup: int,
    native_gate: Any = None,
) -> dict[str, Any]:
    emit_progress("state", f"[{label}] aligning source to {ref}")
    align = remote_align_source(
        container_ip, container_port,
        vllm_ascend_ref=ref,
        vllm_ref=vllm_ref,
    )
    if align.get("status") != "ok":
        raise RuntimeError(f"[{label}] source alignment failed: {align}")

    # Fingerprint native-build inputs right after alignment so a csrc/cmake/
    # requirements change between states is caught before serving (compiled
    # artifacts are not rebuilt by source-only alignment).
    native_digest = remote_native_input_digest(container_ip, container_port)
    emit_progress(
        "native_gate",
        f"[{label}] native input digest={(native_digest.get('digest') or 'unknown')[:16]}",
    )
    if native_gate is not None:
        native_gate(label, native_digest)

    patch_result: dict[str, Any] | None = None
    if args.remote_patch_file:
        patch_result = apply_remote_patch(
            container_ip, container_port, Path(args.remote_patch_file),
        )
        emit_progress("remote_patch", f"[{label}] patch status={patch_result.get('status')}")
        if patch_result.get("status") != "ok":
            raise RuntimeError(f"[{label}] remote patch failed: {patch_result}")

    if args.stale_cleanup:
        pre = safe_stale_cleanup(container_ip, container_port)
        emit_progress("cleanup", f"[{label}] pre-clean matched={pre.get('matched_pids')}")

    try:
        emit_progress("serve", f"[{label}] starting service")
        start = call_serve_start(config)
        if start.get("status") != "ready":
            raise RuntimeError(f"[{label}] service not ready: {str(start)[:1500]}")
        base_url = start["base_url"]
        served_model = start.get("served_model_name") or config.served_model_name or Path(args.model).name

        accuracy: dict[str, Any] | None = None
        if args.accuracy_probe:
            from urllib.parse import urlparse
            serve_port = urlparse(base_url).port or 8000
            emit_progress("accuracy", f"[{label}] running accuracy probe")
            accuracy = run_accuracy_probe(
                container_ip, container_port,
                port=serve_port,
                model=served_model,
                prompt=args.accuracy_prompt,
                max_tokens=args.accuracy_max_tokens,
            )
            emit_progress("accuracy", f"[{label}] probe status={accuracy.get('status')}")

        case_results: list[dict[str, Any]] = []
        for case in cases:
            case_config = replace(config, bench_args=case["bench_args"])
            case_metrics: list[dict[str, Any]] = []
            case_raws: list[dict[str, Any]] = []
            for i in range(max(1, runs)):
                is_warm = i < warmup
                emit_progress(
                    "bench",
                    f"[{label}] case {case['case']} run {i + 1}/{runs}"
                    f"{' (warmup)' if is_warm else ''}",
                )
                raw = run_bench_on_remote(
                    case_config, base_url, served_model, container_ip, container_port,
                )
                case_metrics.append(extract_metrics(raw))
                case_raws.append(raw)
            case_results.append({
                "case": case["case"],
                "request_count": case["request_count"],
                "bench_args": case["bench_args"],
                "per_run": [
                    {"run": j + 1, "warmup": j < warmup, "metrics": m}
                    for j, m in enumerate(case_metrics)
                ],
                "aggregated": _aggregate(case_metrics, warmup),
                "raw_results": case_raws,
            })
    finally:
        call_serve_stop(config, force=True)
        if args.stale_cleanup:
            post = safe_stale_cleanup(container_ip, container_port)
            emit_progress("cleanup", f"[{label}] post-clean matched={post.get('matched_pids')}")

    primary = case_results[0]
    return {
        "label": label,
        "ref": ref,
        "aligned_heads": align.get("heads", {}),
        "native_input_digest": native_digest,
        "remote_patch": patch_result,
        "accuracy_probe": accuracy,
        "runs": runs,
        "warmup_runs": warmup,
        "cases": case_results,
        "per_run": primary["per_run"],
        "aggregated": primary["aggregated"],
        "raw_results": primary["raw_results"],
    }


def _compare(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not states:
        return []
    base_cases = {
        case.get("case"): case
        for case in states[0].get("cases", [])
        if isinstance(case, dict)
    }
    rows: list[dict[str, Any]] = []
    for st in states:
        for case in st.get("cases", []):
            if not isinstance(case, dict):
                continue
            agg = case.get("aggregated", {})
            tpot = agg.get("mean_tpot_ms", {}).get("mean")
            base_tpot = (
                base_cases.get(case.get("case"), {})
                .get("aggregated", {})
                .get("mean_tpot_ms", {})
                .get("mean")
            )
            row: dict[str, Any] = {
                "label": st["label"],
                "case": case.get("case"),
                "request_count": case.get("request_count"),
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

    states: list[dict[str, Any]] = []
    result_paths: list[str] = []
    warnings: list[str] = []
    native_input_changed = False

    try:
        preset = load_preset(args.preset) if args.preset else None

        # --- runs / warmup: CLI > preset > built-in default ---
        runs = args.runs
        if runs is None:
            runs = int(preset["runs"]) if preset and preset.get("runs") is not None else 3
        runs = max(1, runs)
        warmup = args.warmup_runs
        if warmup is None:
            warmup = int(preset["warmup_runs"]) if preset and preset.get("warmup_runs") is not None else 1
        warmup = max(0, min(warmup, runs - 1))

        # --- vllm ref: CLI > preset ---
        vllm_ref = args.vllm_ref or (str(preset["vllm_ref"]) if preset and preset.get("vllm_ref") else None)

        # --- request-count cases: CLI > preset > single default case ---
        request_counts: list[int | None]
        if args.bench_request_counts:
            request_counts = list(args.bench_request_counts)
        elif preset and preset.get("bench_request_counts"):
            request_counts = [int(c) for c in preset["bench_request_counts"]]
        else:
            request_counts = [None]

        # --- fixed dataset parameters: CLI overrides > preset > defaults ---
        preset_fixed = dict(preset.get("fixed_request_dataset") or {}) if preset else {}
        use_fixed = bool(args.fixed_request_dataset or preset_fixed)
        fixed_input_len = args.fixed_input_len or int(preset_fixed.get("input_len") or DEFAULT_FIXED_INPUT_LEN)
        fixed_output_len = args.fixed_output_len or int(preset_fixed.get("output_len") or DEFAULT_FIXED_OUTPUT_LEN)
        fixed_path = (
            args.fixed_dataset_path
            or preset_fixed.get("path")
            or f"/tmp/vaws_bench_fixed_requests_{fixed_input_len}x{fixed_output_len}.jsonl"
        )

        if bench_args is not None:
            base_bench_args = list(bench_args)
        elif preset and preset.get("bench_args"):
            base_bench_args = [str(a) for a in preset["bench_args"]]
        else:
            base_bench_args = []

        config = assemble_config(
            session_id=args.session_id,
            session_file=args.session_file,
            model=args.model,
            tp=args.tp,
            dp=args.dp,
            port=args.port,
            served_model_name=args.served_model_name,
            devices=args.devices,
            health_timeout=args.health_timeout,
            serve_args=serve_args,
            bench_args=base_bench_args,
            extra_env=args.extra_env,
            bench_env=args.bench_env,
            preset=args.preset,
            # States align source directly in-container; parity sync would
            # overwrite the checked-out state, so it is always skipped here.
            skip_parity=True,
        )

        container_ip, container_port = _get_ssh_endpoint(
            session_id=config.session_id,
            session_file=config.session_file,
        )

        cases = build_cases(
            base_bench_args,
            request_counts,
            fixed={"path": fixed_path, "output_len": fixed_output_len} if use_fixed else None,
        )

        # The fixed dataset depends only on model/tokenizer, not on the state,
        # so it is generated once before the state loop.
        fixed_dataset_result: dict[str, Any] | None = None
        if use_fixed:
            num_rows = 1
            for case in cases:
                raw_np = _opt_value(case["bench_args"], "--num-prompts")
                num_rows = max(num_rows, int(raw_np) if raw_np else 64)
            tokenizer_mode = _opt_value(config.serve_args, "--tokenizer-mode") or "auto"
            env_preamble = "".join(
                f"export {k}={shlex.quote(v)}; " for k, v in config.bench_env.items()
            )
            emit_progress(
                "fixed_dataset",
                f"preparing fixed dataset {fixed_path} ({fixed_input_len}/{fixed_output_len}, {num_rows} rows)",
            )
            fixed_dataset_result = prepare_fixed_request_dataset(
                container_ip, container_port,
                model=args.model,
                tokenizer_mode=tokenizer_mode,
                input_len=fixed_input_len,
                output_len=fixed_output_len,
                path=fixed_path,
                num_rows=num_rows,
                prompt=args.fixed_prompt,
                env_preamble=env_preamble,
            )
            emit_progress(
                "fixed_dataset",
                f"dataset ready: sha256={fixed_dataset_result.get('prompt_sha256', '')[:16]}",
            )

        # --- native-input gate: compared against the first state's digest ---
        gate_state: dict[str, Any] = {"digest": None}

        def _native_gate(label: str, digest_result: dict[str, Any]) -> None:
            nonlocal native_input_changed
            digest = (digest_result or {}).get("digest") or ""
            if digest_result.get("status") != "ok" or not digest:
                emit_progress("native_gate", f"[{label}] digest unavailable, gate skipped")
                return
            if gate_state["digest"] is None:
                gate_state["digest"] = digest
                return
            if digest == gate_state["digest"]:
                return
            if args.allow_stale_native:
                native_input_changed = True
                warnings.append(
                    f"NATIVE INPUTS CHANGED: state '{label}' digest {digest} differs from "
                    f"the first state's digest {gate_state['digest']}; compiled custom ops "
                    "were NOT rebuilt, so native-kernel deltas are NOT covered by this comparison."
                )
                emit_progress("native_gate", f"[{label}] WARNING: native inputs changed, continuing (--allow-stale-native)")
                return
            raise RuntimeError(
                f"[{label}] native build inputs (csrc/cmake/requirements) differ from the "
                f"first state (digest {digest} != {gate_state['digest']}) while source "
                "alignment never rebuilds compiled artifacts; the comparison would silently "
                "benchmark stale custom ops. Rebuild via the parity workflow first, or pass "
                "--allow-stale-native to proceed with a warning."
            )

        for label, ref in args.state:
            state_result = _run_one_state(
                args, config, label, ref,
                vllm_ref=vllm_ref,
                cases=cases,
                container_ip=container_ip,
                container_port=container_port,
                runs=runs,
                warmup=warmup,
                native_gate=_native_gate,
            )
            # Persist each state immediately so a later failure never loses
            # completed measurements.
            result_path = write_local_result(config, state_result)
            result_paths.append(str(result_path))
            states.append(state_result)

        result = {
            "status": "ok",
            "session_id": config.session_id,
            "model": args.model,
            "preset": args.preset,
            "vllm_ref": vllm_ref,
            "states": [s["label"] for s in states],
            "comparison": _compare(states),
            "state_results": states,
            "serve_args": serve_args,
            "bench_args": bench_args,
            "warnings": warnings,
            "result_paths": result_paths,
            "fixed_dataset": fixed_dataset_result,
            "timestamp": now_utc(),
        }
        if native_input_changed:
            result["native_input_changed"] = True
        # Persist the combined result under the session benchmark runs dir.
        write_local_result(config, result)
        print_json(result)
        return 0
    except Exception as e:
        print_json({
            "status": "failed",
            "phase": "compare",
            "error": str(e),
            "partial_states": [s["label"] for s in states],
            "result_paths": result_paths,
            "traceback": traceback.format_exc(),
        })
        return 2


if __name__ == "__main__":
    sys.exit(main())
