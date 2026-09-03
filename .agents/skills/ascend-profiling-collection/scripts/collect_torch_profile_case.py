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
    7. write a manifest the analysis skill can consume

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

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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
    unique_collection_run_dir,
)
from profile_control import post_remote_action
from run_remote_analyse import ANALYSE_EXPORT_MODES, analyse_profile_root


DEFAULT_TORCH_PROFILER_DIRNAME = "vllm_profile"
DEFAULT_PROFILE_CONTROL_TIMEOUT = 600
DEFAULT_REQUEST_TIMEOUT = 900
POST_STOP_FLUSH_SECONDS = 5

# Workspace knowledge hooks (local, best-effort):
#   * before collection starts, ``knowledge_preflight_advisories`` queries the
#     workspace knowledge store with "<model> tp<N> <mode>" and records hits in
#     the manifest's ``knowledge_advisories`` field (advisory only);
#   * when a hard-fail gate trips, ``knowledge_failure_matches`` queries
#     known-failure-signatures with the observed error text and attaches the
#     matches (with resolution) to ``manifest.error.knowledge_matches``.
# A missing/invalid knowledge dir degrades both hooks to explicit empty
# arrays with a progress note; collection itself is never blocked.
KNOWLEDGE_DIR = ROOT / ".agents" / "knowledge"
KNOWLEDGE_ADVISORY_KINDS = (
    "model-capabilities",
    "parallelism-compatibility",
    "known-failure-signatures",
)
KNOWLEDGE_QUERY_LIMIT = 3

VL_DEFAULT_IMAGE = (
    ROOT / "vllm-ascend" / "tests" / "e2e" / "310p" / "data" / "qwen.png"
)


# ---------------------------------------------------------------------------
# Workspace knowledge hooks (advisory only, never blocking)
# ---------------------------------------------------------------------------

def _knowledge_api() -> tuple[Any, Any, Any] | None:
    """Lazily import the workspace knowledge API; None when unavailable.

    Importing this skill's ``_common`` already put ``.agents/lib`` on
    sys.path (via the serving skill's common). The import stays lazy so a
    broken/missing lib can never block collection.
    """
    try:
        from vaws_knowledge import (  # type: ignore[import-not-found]
            KnowledgeError,
            get_knowledge_entry,
            query_knowledge,
        )
    except ImportError:
        return None
    return KnowledgeError, query_knowledge, get_knowledge_entry


def knowledge_preflight_advisories(
    model_name: str,
    tp: int,
    mode: str,
    *,
    knowledge_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Query the knowledge store with "<model> tp<N> <mode>" before collecting.

    Returns one advisory per hit (entry_id/kind/summary/score) across
    model-capabilities, parallelism-compatibility and known-failure-signatures
    (limit 3 per kind). Empty array on no hits, empty store, or any
    knowledge-side failure.
    """
    knowledge_dir = knowledge_dir or KNOWLEDGE_DIR
    api = _knowledge_api()
    if api is None:
        emit_progress("knowledge", "vaws_knowledge not importable; preflight advisory skipped")
        return []
    KnowledgeError, query_knowledge, _ = api
    query = f"{model_name} tp{tp} {mode}"
    advisories: list[dict[str, Any]] = []
    try:
        for kind in KNOWLEDGE_ADVISORY_KINDS:
            for match in query_knowledge(
                knowledge_dir=knowledge_dir,
                query=query,
                kinds=[kind],
                limit=KNOWLEDGE_QUERY_LIMIT,
            ):
                advisories.append(
                    {
                        "entry_id": match["id"],
                        "kind": match["kind"],
                        "summary": match["summary"],
                        "score": match["score"],
                    }
                )
    except Exception as exc:  # noqa: BLE001 - advisories must never block collection
        emit_progress("knowledge", f"preflight advisory skipped: {exc}")
        return []
    return advisories


def knowledge_failure_matches(
    signature_text: str,
    *,
    knowledge_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Match an observed failure signature against known-failure-signatures.

    Returns the top-3 matches (entry_id/kind/summary/resolution/score);
    explicit empty array when nothing matches or the store is unusable.
    """
    knowledge_dir = knowledge_dir or KNOWLEDGE_DIR
    api = _knowledge_api()
    if api is None:
        emit_progress("knowledge", "vaws_knowledge not importable; failure-signature lookup skipped")
        return []
    KnowledgeError, query_knowledge, get_knowledge_entry = api
    try:
        matches = query_knowledge(
            knowledge_dir=knowledge_dir,
            query=signature_text,
            kinds=["known-failure-signatures"],
            limit=KNOWLEDGE_QUERY_LIMIT,
        )
        out: list[dict[str, Any]] = []
        for match in matches:
            resolution = ""
            full = get_knowledge_entry(knowledge_dir=knowledge_dir, entry_id=match["id"])
            if full:
                rule = full.get("entry", {}).get("rule", {})
                if isinstance(rule, dict):
                    resolution = str(rule.get("resolution") or "")
            out.append(
                {
                    "entry_id": match["id"],
                    "kind": match["kind"],
                    "summary": match["summary"],
                    "resolution": resolution,
                    "score": match["score"],
                }
            )
    except Exception as exc:  # noqa: BLE001 - failure reporting must not recurse
        emit_progress("knowledge", f"failure-signature lookup skipped: {exc}")
        return []
    return out


def _failure_payload(message: str) -> dict[str, Any]:
    """Manifest ``error`` object: message + knowledge matches for the text."""
    return {
        "message": message,
        "knowledge_matches": knowledge_failure_matches(message),
    }


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


def _parse_sips_dimensions(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to inspect image via sips: {result.stderr[:500]}")
    width = height = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            width = int(line.split(":", 1)[1].strip())
        elif line.startswith("pixelHeight:"):
            height = int(line.split(":", 1)[1].strip())
    if width is None or height is None:
        raise RuntimeError(f"unexpected sips output: {result.stdout}")
    return width, height


def _build_image_data_url(image_path: Path, target_height: int) -> tuple[str, dict[str, Any]]:
    src_w, src_h = _parse_sips_dimensions(image_path)
    if src_h != target_height:
        target_w = max(1, round(src_w * target_height / src_h))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            resized_path = Path(tmp.name)
        result = subprocess.run(
            [
                "sips",
                "--resampleHeightWidth", str(target_height), str(target_w),
                str(image_path),
                "--out", str(resized_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to resize image via sips: {result.stderr[:500]}")
        use_path = resized_path
        final_w, final_h = _parse_sips_dimensions(use_path)
    else:
        use_path = image_path
        final_w, final_h = src_w, src_h

    raw = use_path.read_bytes()
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    meta = {
        "source_path": str(image_path),
        "encoded_path": str(use_path),
        "source_width": src_w,
        "source_height": src_h,
        "encoded_width": final_w,
        "encoded_height": final_h,
    }
    return data_url, meta


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
    if args.session_file:
        serve_args[:0] = ["--session-file", args.session_file]
    else:
        serve_args[:0] = ["--session-id", args.session_id]
    if args.dp is not None and args.dp > 1:
        serve_args.extend(["--dp", str(args.dp)])
    serve_args.extend([
        "--extra-env",
        "PYTORCH_NPU_ALLOC_CONF=expandable_segments:True",
    ])
    if args.skip_parity:
        serve_args.append("--skip-parity")
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
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)

    # Required: target + workload identity
    p.add_argument("--session-id", help="VAWS session id; defaults to the bound session of the current worktree")
    p.add_argument("--session-file", help="explicit session.json path")
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
        "--speculative-method", default="qwen3_5_mtp",
        help="speculative method name; only used when --speculative-tokens > 0",
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

    # Optional: VL workload
    p.add_argument(
        "--image-path", default=None,
        help="path to image used for --request-kind vl; defaults to the "
             "qwen.png test image inside vllm-ascend submodule",
    )
    p.add_argument("--image-height", type=int, default=480,
                   help="resize the image to this pixel height before encoding")

    # Optional: parity opt-out (forwarded to serving)
    p.add_argument("--skip-parity", action="store_true")

    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Resolve the session once (including worktree-binding auto-resolution)
    # and pin it, so every downstream subprocess targets the same session.
    session_target = resolve_execution_target(
        session_id=args.session_id,
        session_file=args.session_file,
    )
    args.session_id = session_target.session_id
    args.session_file = (
        str(session_target.session_file) if session_target.session_file else args.session_file
    )
    machine_alias = session_target.alias

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
        "run_dir": str(run_dir),
        "serve_args": serve_args,
        "image_meta": image_meta,
    }

    service_result: dict[str, Any] | None = None
    stop_result: dict[str, Any] | None = None
    try:
        # Preflight knowledge advisory: known capabilities/compatibility/
        # failure signatures for this exact "<model> tp<N> <mode>" shape.
        # Advisory only -- recorded in the manifest, never blocks collection.
        advisories = knowledge_preflight_advisories(
            args.served_model_name, args.tp, args.mode
        )
        manifest["knowledge_advisories"] = advisories
        emit_progress(
            "knowledge",
            f"preflight advisories: {len(advisories)} knowledge entrie(s) matched",
            advisories=[item["entry_id"] for item in advisories] or None,
        )

        emit_progress("serve_start", f"starting service on session {args.session_id}")
        service_result = call_serve_start(serve_args)
        manifest["service_result"] = service_result
        if service_result.get("status") != "ready":
            raise RuntimeError(f"service did not become ready: {service_result}")

        runtime_dir = service_result["runtime_dir"]
        port = int(service_result["port"])
        profile_root = f"{runtime_dir}/{args.torch_profiler_dir}"

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

        emit_progress("serve_stop", "stopping service")
        stop_result = call_serve_stop(
            session_id=args.session_id,
            session_file=args.session_file,
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
        if stop_result is None:
            try:
                stop_result = call_serve_stop(
                    session_id=args.session_id,
                    session_file=args.session_file,
                )
                manifest["stop_result"] = stop_result
            except Exception:  # noqa: BLE001
                try:
                    stop_result = call_serve_stop(
                        session_id=args.session_id,
                        session_file=args.session_file,
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
