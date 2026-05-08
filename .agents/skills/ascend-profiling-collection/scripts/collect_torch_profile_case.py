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

Failure policy: if any rank's ``kernel_details.csv`` is missing after analyse
(the canonical "device data did not land" case from
``profiling-inventory.md``), the run is reported as failed and exits non-zero
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
    container_endpoint,
    emit_progress,
    ensure_dir,
    now_utc,
    open_local_tunnel,
    print_json,
    resolve_machine,
)
from profile_control import post_remote_action
from run_remote_analyse import analyse_profile_root


DEFAULT_TORCH_PROFILER_DIRNAME = "vllm_profile"
DEFAULT_PROFILE_CONTROL_TIMEOUT = 600
DEFAULT_REQUEST_TIMEOUT = 900
POST_STOP_FLUSH_SECONDS = 5

VL_DEFAULT_IMAGE = (
    ROOT / "vllm-ascend" / "tests" / "e2e" / "310p" / "data" / "qwen.png"
)


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


# ---------------------------------------------------------------------------
# Serving args assembly
# ---------------------------------------------------------------------------

def _build_serve_args(args: argparse.Namespace, profiler_config: dict[str, Any]) -> list[str]:
    serve_args: list[str] = [
        "--machine", args.machine,
        "--model", args.model,
        "--served-model-name", args.served_model_name,
        "--tp", str(args.tp),
    ]
    if args.dp is not None and args.dp > 1:
        serve_args.extend(["--dp", str(args.dp)])
    serve_args.extend([
        "--extra-env",
        "PYTORCH_NPU_ALLOC_CONF=expandable_segments:True",
    ])
    if args.skip_parity:
        serve_args.append("--skip-parity")

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
    p.add_argument("--machine", required=True,
                   help="machine alias or host IP (must be ready in inventory)")
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
    p.add_argument("--request-timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT,
                   help="per chat-completions request timeout (seconds)")
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

    ensure_dir(COLLECTION_STATE_DIR)
    run_dir = COLLECTION_STATE_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{args.tag}"
    ensure_dir(run_dir)

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
        "machine": args.machine,
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
        "prompt_tokens": args.prompt_tokens,
        "benchmark_output_tokens": args.benchmark_output_tokens,
        "followup_output_tokens": args.followup_output_tokens,
        "benchmark_total_requests": args.benchmark_total_requests,
        "benchmark_concurrency": args.benchmark_concurrency,
        "profile_control_timeout": args.profile_control_timeout,
        "run_dir": str(run_dir),
        "serve_args": serve_args,
        "image_meta": image_meta,
    }

    service_result: dict[str, Any] | None = None
    stop_result: dict[str, Any] | None = None
    try:
        emit_progress("serve_start", f"starting service on {args.machine}")
        service_result = call_serve_start(serve_args)
        manifest["service_result"] = service_result
        if service_result.get("status") != "ready":
            raise RuntimeError(f"service did not become ready: {service_result}")

        runtime_dir = service_result["runtime_dir"]
        port = int(service_result["port"])
        profile_root = f"{runtime_dir}/{args.torch_profiler_dir}"

        record = resolve_machine(args.machine)
        ep = container_endpoint(record)

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

            emit_progress("profile_control", "POST /stop_profile")
            manifest["stop_profile"] = post_remote_action(
                ep, port, "stop_profile", args.profile_control_timeout,
            )

        # Give multi-rank torch profiler a small window to flush trailing data
        # before the service is torn down. /stop_profile usually blocks until
        # done, but profiler thread shutdown has historically lagged.
        time.sleep(POST_STOP_FLUSH_SECONDS)

        emit_progress("serve_stop", "stopping service")
        stop_result = call_serve_stop(args.machine)
        manifest["stop_result"] = stop_result

        emit_progress("analyse", f"analysing {profile_root}")
        analyse_bundle = analyse_profile_root(ep, profile_root)
        manifest["remote_profile_root"] = profile_root
        manifest["remote_profile_dirs"] = analyse_bundle["dirs"]
        manifest["analysis_status"] = analyse_bundle["analysis_status"]
        manifest["completed_at"] = now_utc()

        # Hard gate: degenerate roots fail loudly so downstream analyze.py is
        # never asked to interpret them.
        worst = analyse_bundle["analysis_status"]
        if worst == "ok":
            manifest["status"] = "ok"
            (run_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print_json(manifest)
            return 0

        manifest["status"] = "failed"
        manifest["error"] = (
            f"profiling collection produced incomplete output (analysis_status="
            f"{worst}); re-collect required, see profiling-inventory.md"
        )
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print_json(manifest)
        return 1

    except Exception as exc:  # noqa: BLE001
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        manifest["failed_at"] = now_utc()
        if stop_result is None:
            try:
                stop_result = call_serve_stop(args.machine)
                manifest["stop_result"] = stop_result
            except Exception:  # noqa: BLE001
                try:
                    stop_result = call_serve_stop(args.machine, force=True)
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
