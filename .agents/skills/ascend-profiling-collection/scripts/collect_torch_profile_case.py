#!/usr/bin/env python3
"""Collect one torch-profiler case on a workspace-managed remote NPU container.

This is the single agent-facing entry point for the
``ascend-profiling-collection`` skill. It chains together what other skills
already provide:

    1. start a service via vllm-ascend-serving with ``--profiler-config``
    2. flip ``/start_profile`` (profile_control.py)
    3. send a benchmark wave + one follow-up tail request
    4. flip ``/stop_profile`` (profile_control.py)
    5. stop the service via vllm-ascend-serving
    6. analyse every ``*_ascend_pt`` directory and verify outputs
       (run_remote_analyse.py)
    7. optionally archive each rank's outputs to shared storage
       (``--archive-dir``)
    8. write a manifest the analysis skill can consume

The skill never modifies code in serving / parity / benchmark; it only
orchestrates them. The serving skill stays profiling-agnostic -- it only
forwards ``--profiler-config`` to ``vllm serve``.

Failure policy: if any rank's expected analyse output is missing after
analyse (the per-rank ``ascend_pytorch_profiler_*.db`` in the default db
export mode; ``kernel_details.csv`` in text/both mode -- the canonical
"device data did not land" case from ``references/behavior.md`` "Output
verification"), the run is reported as failed and exits non-zero
even though every previous step succeeded. Downstream analysis must not
process degenerate roots silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]
_LIB = _ROOT / ".agents" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=_ROOT)

import argparse
import base64
import json
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import (
    COLLECTION_STATE_DIR,
    ROOT,
    call_serve_start,
    call_serve_stop,
    emit_progress,
    ensure_dir,
    now_utc,
    open_local_tunnel,
    print_json,
    resolve_execution_target,
    safe_run_token,
    ssh_exec,
    unique_collection_run_dir,
)
from profile_control import post_remote_action
from run_remote_analyse import ANALYSE_EXPORT_MODES, analyse_profile_root


DEFAULT_TORCH_PROFILER_DIRNAME = "vllm_profile"
DEFAULT_PROFILE_CONTROL_TIMEOUT = 600
DEFAULT_REQUEST_TIMEOUT = 900
POST_STOP_FLUSH_SECONDS = 5

VL_DEFAULT_IMAGE = (
    ROOT / "vllm-ascend" / "tests" / "e2e" / "310p" / "data" / "qwen.png"
)


def _failure_payload(message: str) -> dict[str, Any]:
    """Keep the observed error available without starting another service."""
    return {"message": message}


# ---------------------------------------------------------------------------
# Workload payload helpers (multimodal + text)
# ---------------------------------------------------------------------------

@dataclass
class RequestResult:
    index: int
    ok: bool
    status: int | None
    latency_sec: float
    body: dict[str, Any] | None
    error: str | None


def _post_json(url: str, payload: dict[str, Any], timeout: int) -> tuple[int, bytes]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _build_image_data_url(image_path: Path, target_height: int) -> tuple[str, dict[str, Any]]:
    from io import BytesIO
    from PIL import Image, ImageOps
    if target_height < 1:
        raise ValueError("image height must be positive")
    with Image.open(image_path) as source:
        corrected = ImageOps.exif_transpose(source)
        src_w, src_h = corrected.size
        final_w = max(1, round(src_w * target_height / src_h))
        encoded = corrected.convert("RGB").resize((final_w, target_height), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        encoded.save(buffer, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    return data_url, {"source_path": str(image_path), "source_width": src_w, "source_height": src_h,
                      "encoded_width": final_w, "encoded_height": target_height, "encoding": "PNG"}


def _build_long_text(token_count: int, *, prefix: str, request_index: int) -> str:
    filler = " ".join(["hello"] * token_count)
    return f"{prefix}\nRequest-{request_index:03d}\n{filler}"


def _build_chat_payload(
    *,
    model: str,
    prompt_text: str,
    max_tokens: int,
    image_url: str | None,
) -> dict[str, Any]:
    if image_url:
        content: list[dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": image_url}},
            {"type": "text", "text": prompt_text},
        ]
    else:
        content = [{"type": "text", "text": prompt_text}]
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "top_p": 1,
    }


def _send_chat_request(
    *,
    base_url: str,
    model: str,
    prompt_text: str,
    max_tokens: int,
    image_url: str | None,
    index: int,
    timeout: int,
) -> RequestResult:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = _build_chat_payload(
        model=model, prompt_text=prompt_text,
        max_tokens=max_tokens, image_url=image_url,
    )
    start = time.time()
    try:
        status, raw = _post_json(url, payload, timeout=timeout)
        latency = time.time() - start
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            body = {"raw": raw.decode("utf-8", errors="replace")[:2000]}
        return RequestResult(
            index=index, ok=200 <= status < 300, status=status,
            latency_sec=latency, body=body, error=None,
        )
    except urllib.error.HTTPError as exc:
        latency = time.time() - start
        return RequestResult(
            index=index, ok=False, status=exc.code,
            latency_sec=latency, body=None,
            error=exc.read().decode("utf-8", errors="replace")[:2000],
        )
    except Exception as exc:  # noqa: BLE001
        latency = time.time() - start
        return RequestResult(
            index=index, ok=False, status=None,
            latency_sec=latency, body=None, error=str(exc),
        )


def _run_benchmark_wave(
    *,
    base_url: str,
    model: str,
    total_requests: int,
    concurrency: int,
    input_tokens: int,
    output_tokens: int,
    image_url: str | None,
    prompt_prefix: str,
    request_timeout: int,
) -> list[RequestResult]:
    results: list[RequestResult] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = []
        for idx in range(total_requests):
            prompt_text = _build_long_text(
                input_tokens, prefix=prompt_prefix, request_index=idx,
            )
            futures.append(pool.submit(
                _send_chat_request,
                base_url=base_url, model=model,
                prompt_text=prompt_text, max_tokens=output_tokens,
                image_url=image_url, index=idx, timeout=request_timeout,
            ))
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item.index)
    return results


def _render_request_results(results: list[RequestResult]) -> list[dict[str, Any]]:
    return [
        {
            "index": item.index,
            "ok": item.ok,
            "status": item.status,
            "latency_sec": round(item.latency_sec, 4),
            "error": item.error,
            "body": item.body,
        }
        for item in results
    ]


def _evaluate_workload(
    bench_results: list[RequestResult],
    followup_result: RequestResult | None,
    threshold: float,
) -> dict[str, Any]:
    """Decide whether the workload was healthy enough to make the trace useful.

    Hard-fails (returned status != "ok") propagate to the top-level manifest
    status so downstream analysis never sees a profiling root that was
    captured with no actual model traffic flowing through it.
    """
    bench_total = len(bench_results)
    bench_ok = sum(1 for r in bench_results if r.ok)
    rate = (bench_ok / bench_total) if bench_total else 0.0
    followup_ok = bool(followup_result and followup_result.ok)

    if not followup_ok:
        status = "followup_failed"
    elif bench_total == 0:
        status = "no_benchmark_requests"
    elif rate < threshold:
        status = "benchmark_below_threshold"
    else:
        status = "ok"

    return {
        "status": status,
        "bench_total": bench_total,
        "bench_ok": bench_ok,
        "bench_success_rate": round(rate, 4),
        "bench_threshold": threshold,
        "followup_ok": followup_ok,
    }


# ---------------------------------------------------------------------------
# Serving args assembly
# ---------------------------------------------------------------------------

def _build_serve_args(args: argparse.Namespace, profiler_config: dict[str, Any]) -> list[str]:
    serve_args: list[str] = [
        "--model", args.model,
        "--served-model-name", args.served_model_name,
        "--tp", str(args.tp),
    ]
    if getattr(args, "context_file", None):
        serve_args[:0] = ["--context-file", args.context_file]
    if getattr(args, "service", None):
        serve_args[:0] = ["--service", args.service]
    if args.dp is not None and args.dp > 1:
        serve_args.extend(["--dp", str(args.dp)])
    serve_args.extend([
        "--extra-env",
        "PYTORCH_NPU_ALLOC_CONF=expandable_segments:True",
    ])
    if args.health_timeout is not None:
        serve_args.extend(["--health-timeout", str(args.health_timeout)])

    serve_args.append("--")
    serve_args.extend([
        "--max-model-len", str(args.max_model_len),
        "--trust-remote-code",
        "--gpu-memory-utilization", str(args.gpu_memory_utilization),
        "--max-num-seqs", str(args.max_num_seqs),
        "--max-num-batched-tokens", str(args.max_num_batched_tokens),
    ])
    if args.api_server_count is not None:
        serve_args.extend(["--api-server-count", str(args.api_server_count)])

    serve_args.extend([
        "--profiler-config", json.dumps(profiler_config, separators=(",", ":")),
    ])

    if args.speculative_tokens > 0:
        serve_args.extend([
            "--speculative-config",
            json.dumps(
                {"method": args.speculative_method,
                 "num_speculative_tokens": args.speculative_tokens},
                separators=(",", ":"),
            ),
        ])

    if args.enable_expert_parallel:
        serve_args.append("--enable-expert-parallel")

    if args.mode == "enforce_eager":
        serve_args.append("--enforce-eager")
    elif args.mode == "full_decode_only":
        serve_args.extend([
            "--compilation-config",
            json.dumps({"cudagraph_mode": "FULL_DECODE_ONLY"}, separators=(",", ":")),
        ])
    elif args.mode == "piecewise_graph":
        serve_args.extend([
            "--compilation-config",
            json.dumps({"cudagraph_mode": "PIECEWISE"}, separators=(",", ":")),
        ])
    else:
        raise ValueError(f"unknown --mode: {args.mode}")

    return serve_args


# ---------------------------------------------------------------------------
# Archive to shared storage (optional --archive-dir)
#
# Profiling outputs are far too large to pull back to the local Mac (a single
# dsv3.1 analysis dragged back 2.2GB once). When --archive-dir points at the
# shared-storage filesystem mounted on every managed host/container (e.g.
# /mnt/weight/<user>/profiling/archives), each rank's analyse outputs are
# copied *on the container* (cp -r over the existing ssh channel, ranks
# serially) into:
#
#     <archive-dir>/<tag>_<compact started_at>/<rank-dir-basename>/
#         ASCEND_PROFILER_OUTPUT/     (db + csv exports, self-contained)
#         profiler_info_*.json
#         profiler_metadata.json
#
# The rank-dir basename is kept as the subdirectory name, so the archive root
# itself is a valid profiling root full of ``*_ascend_pt`` directories and
# can be fed straight to the analysis skill's ``--remote-profile-root`` from
# any machine that mounts the same shared storage.
#
# Failure semantics: archiving runs only after analyse+verify passed with all
# ranks ok, and a copy failure never flips an already-ok collection to
# failed -- it is recorded in ``manifest.archive_error`` (with the affected
# rank's ``outputs.archived_path`` left null) plus a stderr warning.
# ---------------------------------------------------------------------------

# torch-profiler metadata files written next to ASCEND_PROFILER_OUTPUT/ in
# each rank dir; copied best-effort (a missing one does not fail the rank).
ARCHIVE_METADATA_NAMES = ("profiler_info_*.json", "profiler_metadata.json")
ARCHIVE_COPY_TIMEOUT_S = 1800


def compact_utc_timestamp(utc_iso: str) -> str:
    """"2026-09-07T03:36:45Z" -> "20260907T033645Z" (dir-name safe)."""
    return utc_iso.replace("-", "").replace(":", "")


def archive_run_dir_name(tag: str, started_at: str) -> str:
    """``<safe-tag>_<compact started_at>`` archive root directory name."""
    return f"{safe_run_token(tag, fallback='profile')}_{compact_utc_timestamp(started_at)}"


def build_rank_archive_script(rank_dir: str, dest_dir: str) -> str:
    """Remote bash: copy one rank's analyse outputs into ``dest_dir``.

    ``ASCEND_PROFILER_OUTPUT/`` is copied whole (in db mode it carries the
    per-rank db; there is no csv to cherry-pick) and must exist -- verify has
    already guaranteed that, so a copy failure here is a real archive error.
    The metadata files are best-effort so a missing ``profiler_metadata.json``
    does not fail the rank.
    """
    src = rank_dir.rstrip("/")
    # Quote the directory part only: quoting the whole path would turn
    # ``profiler_info_*.json`` into a literal string and kill glob expansion.
    meta = " ".join(f"{shlex.quote(src)}/{name}" for name in ARCHIVE_METADATA_NAMES)
    quoted_dest = shlex.quote(dest_dir)
    return (
        "set -e; "
        f"mkdir -p {quoted_dest}; "
        f"cp -r {shlex.quote(src + '/ASCEND_PROFILER_OUTPUT')} {quoted_dest}/; "
        f"for f in {meta}; do "
        f"if [ -e \"$f\" ]; then cp \"$f\" {quoted_dest}/; fi; "
        "done"
    )


def archive_rank_outputs(
    ep,
    rank_dirs: list[str],
    archive_dir: str,
    *,
    tag: str,
    started_at: str,
    copy_timeout: float = ARCHIVE_COPY_TIMEOUT_S,
) -> dict[str, Any]:
    """Archive every rank's outputs under ``<archive_dir>/<tag>_<ts>/``.

    Serial across ranks and never raises: per-rank copy failures are captured
    in ``archive_error`` so an archive problem cannot overturn an already-ok
    collection. Returns::

        {
          "archive_dir": "<archive_dir>/<tag>_<ts>",
          "archived": bool,            # True only when every rank copied
          "archive_error": str | None,
          "ranks": [{"path": rank_dir, "archived_path": str | None}, ...],
        }
    """
    root = f"{archive_dir.rstrip('/')}/{archive_run_dir_name(tag, started_at)}"
    result: dict[str, Any] = {
        "archive_dir": root,
        "archived": False,
        "archive_error": None,
        "ranks": [],
    }
    errors: list[str] = []
    for rank_dir in rank_dirs:
        basename = rank_dir.rstrip("/").rsplit("/", 1)[-1]
        dest = f"{root}/{basename}"
        entry: dict[str, Any] = {"path": rank_dir, "archived_path": None}
        try:
            proc = ssh_exec(
                ep,
                build_rank_archive_script(rank_dir, dest),
                check=False,
                timeout=copy_timeout,
            )
            if proc.returncode == 0:
                entry["archived_path"] = dest
            else:
                errors.append(
                    f"{rank_dir}: rc={proc.returncode} {proc.stderr[-300:]}"
                )
        except Exception as exc:  # noqa: BLE001 - archive must not fail collection
            errors.append(f"{rank_dir}: {exc}")
        result["ranks"].append(entry)
    result["archived"] = not errors
    if errors:
        result["archive_error"] = "; ".join(errors)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)

    # Required: target + workload identity
    p.add_argument("--context-file", help="VAWS task context; defaults to VAWS_CONTEXT_FILE")
    p.add_argument("--execution-id", help="live service execution; omit to start a new one")
    p.add_argument("--service", default="vllm")
    p.add_argument("--model", required=True,
                   help="absolute remote path to model weights")
    p.add_argument("--served-model-name", required=True,
                   help="name vLLM exposes via /v1/models")
    p.add_argument("--tp", type=int, required=True, help="tensor-parallel size")
    p.add_argument("--tag", required=True,
                   help="stable identifier for this collection run; used in run dir name")
    p.add_argument(
        "--mode",
        required=True,
        choices=("enforce_eager", "full_decode_only", "piecewise_graph"),
        help="graph mode for the service",
    )
    p.add_argument(
        "--request-kind",
        required=True,
        choices=("text", "vl"),
        help="workload kind sent during the profile window",
    )
    p.add_argument("--benchmark-output-tokens", type=int, required=True,
                   help="max_tokens per benchmark-wave request")

    # Optional: parallelism / speculative / EP
    p.add_argument("--dp", type=int, default=None,
                   help="data-parallel size (forwarded to serving as --dp)")
    p.add_argument("--enable-expert-parallel", action="store_true")
    p.add_argument(
        "--speculative-tokens", type=int, default=0,
        help="num_speculative_tokens; 0 disables --speculative-config",
    )
    p.add_argument(
        "--speculative-method", default="mtp",
        help="speculative method name; only used when --speculative-tokens > 0. "
             "Default 'mtp' is vLLM's canonical generic MTP method (model-specific "
             "aliases like 'qwen3_5_mtp' are deprecated and remapped); the old "
             "qwen3_5_mtp default built a drafter expecting Qwen3.5-style mtp_block "
             "bias weights and crashed DeepSeek MTP checkpoints.",
    )

    # Optional: vLLM serving knobs
    p.add_argument("--gpu-memory-utilization", type=float, default=0.85)
    p.add_argument("--max-model-len", type=int, default=4096)
    p.add_argument("--max-num-seqs", type=int, default=2)
    p.add_argument("--max-num-batched-tokens", type=int, default=1024)
    p.add_argument(
        "--api-server-count", type=int, default=None,
        help="override vLLM --api-server-count (for isolating multi-api-server issues)",
    )

    # Optional: workload shape during the profile window
    p.add_argument("--prompt-tokens", type=int, default=2000,
                   help="input length for benchmark wave + follow-up")
    p.add_argument("--followup-output-tokens", type=int, default=5,
                   help="max_tokens for the single tail request")
    p.add_argument("--benchmark-total-requests", type=int, default=10)
    p.add_argument("--benchmark-concurrency", type=int, default=5)
    p.add_argument(
        "--benchmark-success-threshold",
        type=float,
        default=0.8,
        help=(
            "minimum required success rate of the benchmark wave (0..1); "
            "below this the run is reported as failed because the trace was "
            "captured without real model traffic. The follow-up request is "
            "always required to succeed independently of this threshold"
        ),
    )
    p.add_argument("--request-timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT,
                   help="per chat-completions request timeout (seconds)")
    p.add_argument(
        "--health-timeout", type=int, default=None,
        help=("passthrough to serve_start --health-timeout; large quantized "
              "models loading from shared storage routinely exceed the 300s "
              "default while still making progress"),
    )
    p.add_argument(
        "--profile-control-timeout", type=int,
        default=DEFAULT_PROFILE_CONTROL_TIMEOUT,
        help=("timeout for /start_profile and /stop_profile; multi-rank "
              "torch profiler setup/finalization can take much longer than "
              "an ordinary request"),
    )

    # Optional: profiler depth
    p.add_argument("--torch-profiler-dir", default=DEFAULT_TORCH_PROFILER_DIRNAME,
                   help="relative dir under runtime_dir where vLLM writes traces")
    p.add_argument("--torch-profiler-with-stack", action="store_true")

    # Optional: analyse() export shape
    p.add_argument(
        "--analyse-export",
        choices=ANALYSE_EXPORT_MODES,
        default="db",
        help=(
            "export_type passed to torch_npu analyse(): 'db' (default) writes "
            "only ascend_pytorch_profiler_*.db per rank (the analysis skill "
            "rebuilds the kernel event stream from it); 'text' writes the "
            "historical kernel_details.csv + trace_view.json; 'both' writes "
            "everything"
        ),
    )

    # Optional: archive to shared storage
    p.add_argument(
        "--archive-dir",
        default=None,
        metavar="<remote-path>",
        help=(
            "remote shared-storage directory (e.g. "
            "/mnt/weight/<user>/profiling/archives) to archive each rank's "
            "analyse outputs into after analyse+verify passes: every rank's "
            "ASCEND_PROFILER_OUTPUT/ + profiler metadata is copied (on the "
            "container, ranks serially) to "
            "<archive-dir>/<tag>_<started_at>/<rank-dir-basename>/. The "
            "archive root is itself a valid profiling root for the analysis "
            "skill's --remote-profile-root on any machine mounting the same "
            "storage. Archive copy failures never fail an already-ok "
            "collection; they are recorded as manifest.archive_error"
        ),
    )

    # Optional: VL workload
    p.add_argument(
        "--image-path", default=None,
        help="path to image used for --request-kind vl; defaults to the "
             "qwen.png test image inside vllm-ascend submodule",
    )
    p.add_argument("--image-height", type=int, default=480,
                   help="resize the image to this pixel height before encoding")

    # Optional: parity opt-out (forwarded to serving)
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    machine_alias = getattr(args, "service", None) or "vllm"
    args.session_id = machine_alias
    args.session_file = None

    run_dir = unique_collection_run_dir(
        tag=args.tag,
        session_id=args.session_id,
        machine=machine_alias,
    )

    prompt_prefix = (
        "Please describe the image and also summarize the long text context."
        if args.request_kind == "vl"
        else "Please continue the following long text."
    )

    image_url: str | None = None
    image_meta: dict[str, Any] | None = None
    if args.request_kind == "vl":
        image_path = Path(args.image_path) if args.image_path else VL_DEFAULT_IMAGE
        if not image_path.exists():
            print_json({
                "status": "failed",
                "error": f"image path does not exist: {image_path}",
                "tag": args.tag,
            })
            return 2
        image_url, image_meta = _build_image_data_url(image_path, args.image_height)

    profiler_config = {
        "profiler": "torch",
        "torch_profiler_dir": f"./{args.torch_profiler_dir}",
        "torch_profiler_with_stack": bool(args.torch_profiler_with_stack),
    }

    serve_args = _build_serve_args(args, profiler_config)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "started_at": now_utc(),
        "tag": args.tag,
        "machine": machine_alias,
        "session_id": args.session_id,
        "session_file": args.session_file,
        "model": args.model,
        "served_model_name": args.served_model_name,
        "tp": args.tp,
        "dp": args.dp,
        "mode": args.mode,
        "request_kind": args.request_kind,
        "speculative_tokens": args.speculative_tokens,
        "speculative_method": (
            args.speculative_method if args.speculative_tokens > 0 else None
        ),
        "enable_expert_parallel": bool(args.enable_expert_parallel),
        "api_server_count": args.api_server_count,
        "torch_profiler_with_stack": bool(args.torch_profiler_with_stack),
        "torch_profiler_dir": args.torch_profiler_dir,
        "analyse_export": args.analyse_export,
        "prompt_tokens": args.prompt_tokens,
        "benchmark_output_tokens": args.benchmark_output_tokens,
        "followup_output_tokens": args.followup_output_tokens,
        "benchmark_total_requests": args.benchmark_total_requests,
        "benchmark_concurrency": args.benchmark_concurrency,
        "benchmark_success_threshold": args.benchmark_success_threshold,
        "expected_ranks": args.tp * (args.dp if args.dp else 1),
        "profile_control_timeout": args.profile_control_timeout,
        "archive_dir": None,
        "archived": False,
        "run_dir": str(run_dir),
        "serve_args": serve_args,
        "image_meta": image_meta,
    }

    service_result: dict[str, Any] | None = None
    stop_result: dict[str, Any] | None = None
    started_service = False
    try:
        started_service = False
        if args.execution_id:
            emit_progress("serve_start", f"using live execution {args.execution_id}")
            session_target = resolve_execution_target(
                context_file=args.context_file,
                execution_id=args.execution_id,
                service=args.service,
            )
            from vaws_task_target import execution_target, task_client
            routed = execution_target(task_client(args.context_file), str(args.execution_id))
            service_result = {
                "status": "ready",
                "execution_id": args.execution_id,
                "port": routed.get("service_port"),
            }
        else:
            emit_progress("serve_start", f"starting service {args.service}")
            service_result = call_serve_start(serve_args)
            started_service = True
        manifest["service_result"] = service_result
        if service_result.get("status") != "ready" and not args.execution_id:
            raise RuntimeError(f"service did not become ready: {service_result}")
        if not args.execution_id:
            session_target = resolve_execution_target(
                context_file=args.context_file,
                execution_id=service_result.get("execution_id"),
                service=args.service,
            )
        port = int(service_result.get("port") or 0)
        if not port:
            raise RuntimeError("service has no port")
        cwd = session_target.cwd or "/vllm-workspace"
        profile_root = f"{cwd.rstrip('/')}/{args.torch_profiler_dir}"
        args.session_id = session_target.task_id or args.session_id

        ep = session_target.endpoint

        with open_local_tunnel(ep, port) as tunnel:
            manifest["request_tunnel"] = tunnel
            request_base_url = tunnel["base_url"]

            emit_progress("profile_control", "POST /start_profile")
            manifest["start_profile"] = post_remote_action(
                ep, port, "start_profile", args.profile_control_timeout,
            )

            emit_progress(
                "workload",
                f"benchmark wave: {args.benchmark_total_requests} req @ "
                f"concurrency {args.benchmark_concurrency}",
            )
            bench_results = _run_benchmark_wave(
                base_url=request_base_url,
                model=args.served_model_name,
                total_requests=args.benchmark_total_requests,
                concurrency=args.benchmark_concurrency,
                input_tokens=args.prompt_tokens,
                output_tokens=args.benchmark_output_tokens,
                image_url=image_url,
                prompt_prefix=prompt_prefix,
                request_timeout=args.request_timeout,
            )
            manifest["benchmark_results"] = _render_request_results(bench_results)

            emit_progress("workload", "follow-up single request")
            followup_prompt = _build_long_text(
                args.prompt_tokens,
                prefix=prompt_prefix + "\nFollow-up request.",
                request_index=args.benchmark_total_requests,
            )
            followup_result = _send_chat_request(
                base_url=request_base_url,
                model=args.served_model_name,
                prompt_text=followup_prompt,
                max_tokens=args.followup_output_tokens,
                image_url=image_url,
                index=args.benchmark_total_requests,
                timeout=args.request_timeout,
            )
            manifest["followup_result"] = _render_request_results([followup_result])[0]

            workload_status = _evaluate_workload(
                bench_results, followup_result, args.benchmark_success_threshold,
            )
            manifest["workload_status"] = workload_status

            emit_progress("profile_control", "POST /stop_profile")
            manifest["stop_profile"] = post_remote_action(
                ep, port, "stop_profile", args.profile_control_timeout,
            )

        # Give multi-rank torch profiler a small window to flush trailing data
        # before the service is torn down. /stop_profile usually blocks until
        # done, but profiler thread shutdown has historically lagged.
        time.sleep(POST_STOP_FLUSH_SECONDS)

        stop_result = {"status": "left_running"}
        if started_service:
            emit_progress("serve_stop", "stopping service")
            stop_result = call_serve_stop(
                context_file=getattr(args, "context_file", None),
                execution_id=service_result.get("execution_id"),
                service=getattr(args, "service", None),
            )
        manifest["stop_result"] = stop_result

        expected_ranks = manifest["expected_ranks"]
        emit_progress(
            "analyse",
            f"analysing {profile_root} (expected_ranks={expected_ranks}, "
            f"export={args.analyse_export})",
        )
        analyse_bundle = analyse_profile_root(
            ep, profile_root, expected_ranks=expected_ranks,
            analyse_export=args.analyse_export,
        )
        manifest["remote_profile_root"] = profile_root
        manifest["remote_profile_dirs"] = analyse_bundle["dirs"]
        # Schema stability: every rank's outputs carries archived_path (null
        # until/unless --archive-dir archiving fills it in).
        for item in manifest["remote_profile_dirs"]:
            item["outputs"]["archived_path"] = None
        manifest["rank_count"] = analyse_bundle.get("rank_count")
        manifest["analysis_status"] = analyse_bundle["analysis_status"]
        manifest["expected_output_kind"] = analyse_bundle.get("expected_output_kind")
        manifest["analyse_wall_s"] = analyse_bundle.get("analyse_wall_s")
        manifest["analyse_parallelism"] = analyse_bundle.get("analyse_parallelism")
        manifest["completed_at"] = now_utc()

        # Hard gate: degenerate roots OR a workload that did not actually
        # exercise the model both fail loudly so downstream analyze.py is
        # never asked to interpret a useless trace.
        analysis_worst = analyse_bundle["analysis_status"]
        workload_worst = manifest["workload_status"]["status"]
        if analysis_worst == "ok" and workload_worst == "ok":
            manifest["status"] = "ok"
            # Optional archive to shared storage: runs only after every rank
            # verified ok, and a copy failure never overturns this ok status
            # (it lands in archive_error + a stderr warning instead).
            if args.archive_dir:
                emit_progress(
                    "archive",
                    f"archiving rank outputs under {args.archive_dir}",
                )
                archive_result = archive_rank_outputs(
                    ep,
                    [d["path"] for d in analyse_bundle["dirs"]],
                    args.archive_dir,
                    tag=args.tag,
                    started_at=manifest["started_at"],
                )
                manifest["archive_dir"] = archive_result["archive_dir"]
                manifest["archived"] = archive_result["archived"]
                if archive_result["archive_error"]:
                    manifest["archive_error"] = archive_result["archive_error"]
                    emit_progress(
                        "archive",
                        "archive copy failed (collection stays ok): "
                        + archive_result["archive_error"],
                    )
                archived_by_rank = {
                    r["path"]: r["archived_path"] for r in archive_result["ranks"]
                }
                for item in manifest["remote_profile_dirs"]:
                    item["outputs"]["archived_path"] = archived_by_rank.get(
                        item["path"]
                    )
            (run_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print_json(manifest)
            return 0

        reasons: list[str] = []
        if analysis_worst != "ok":
            reasons.append(f"analysis_status={analysis_worst}")
        if workload_worst != "ok":
            reasons.append(f"workload_status={workload_worst}")
        manifest["status"] = "failed"
        manifest["error"] = _failure_payload(
            "profiling collection produced an unusable trace ("
            + "; ".join(reasons)
            + "); re-collect required, see SKILL.md Failure policy"
        )
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print_json(manifest)
        return 1

    except Exception as exc:  # noqa: BLE001
        manifest["status"] = "failed"
        manifest["error"] = _failure_payload(str(exc))
        manifest["failed_at"] = now_utc()
        if stop_result is None and started_service:
            try:
                stop_result = call_serve_stop(
                    context_file=getattr(args, "context_file", None),
                    execution_id=service_result.get("execution_id") if service_result else None,
                    service=getattr(args, "service", None),
                    force=True,
                )
                manifest["stop_result"] = stop_result
            except Exception as stop_exc:  # noqa: BLE001
                manifest["stop_error"] = str(stop_exc)

        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print_json(manifest)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
