#!/usr/bin/env python3
"""
Ascend Memory Profiling -- Data Collection Orchestrator.

Two modes of operation:

**Standalone mode** (default):
  Phase 0: npu-smi baseline (no user process)
  Phase 1: Start vLLM serve with msprof wrapping (mandatory)
  Phase 2: npu-smi after service ready + vLLM startup logs
  Phase 3: Send inference requests + npu-smi during inference
  Phase 4: Stop service, msprof flushes
  Phase 5: msprof export → CSV files
  Phase 6: Save all raw data locally

**Attach mode** (--attach):
  Attach to a service already managed by the vllm-ascend-serving skill.
  Reads service state (port, PID, log paths, model config) from
  `.vaws-local/serving/<alias>.json`.  Skips service start/stop.
  If the service was launched with --wrap-script pointing to the msprof
  wrapper, attach mode detects this, runs msprof export, and collects CSVs.
  When the service was NOT launched with msprof, a warning is emitted and the
  report will mark component-level memory as untraceable.

Progress on stderr, final JSON manifest on stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import time
from pathlib import Path

from _common import (
    ENV_PREAMBLE,
    MSPROF_WRAPPER_REMOTE_PATH,
    SshEndpoint,
    check_msprof_available,
    endpoint_from_machine,
    ensure_run_dir,
    find_python,
    get_machine_alias,
    load_serving_state,
    progress,
    resolve_machine,
    run_msprof_export,
    ssh_exec,
    ssh_upload,
    ssh_write_text,
    upload_msprof_wrapper,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Collect Ascend NPU memory profiling data")
    p.add_argument("--machine", required=True, help="Machine alias or IP")
    p.add_argument("--model", default="", help="Remote model weight path (auto-detected in attach mode)")
    p.add_argument("--tp", type=int, default=None, help="Tensor parallel size (auto-detected in attach mode)")
    p.add_argument("--dp", type=int, default=None, help="Data parallel size (auto-detected in attach mode)")
    p.add_argument("--devices", default="", help="Comma-separated device IDs (default: auto)")
    p.add_argument("--gpu-memory-utilization", type=float, default=0.85)
    p.add_argument("--max-model-len", type=int, default=2048)
    p.add_argument("--port", type=int, default=None, help="Service port (auto-detected in attach mode)")
    p.add_argument("--enable-expert-parallel", action="store_true")
    p.add_argument("--enforce-eager", action="store_true", default=False)
    p.add_argument("--max-tokens", type=int, default=128, help="Max tokens per inference request")
    p.add_argument("--prompt", default="Explain transformer attention mechanism in detail.",
                   help="Prompt for inference request")
    p.add_argument("--image-url", default="", help="Image URL for VL model inference (triggers chat completion)")
    p.add_argument("--tag", default="", help="Tag for the output directory name")
    p.add_argument("--health-timeout", type=int, default=300, help="Seconds to wait for service ready")
    p.add_argument("--msprof-mem-freq", type=int, default=50, help="msprof hardware memory sampling freq (Hz)")
    p.add_argument("--speculative-config", default="", help="JSON string for SpeculativeConfig (e.g. MTP)")
    p.add_argument("--compilation-config", default="", help="JSON string for CompilationConfig (e.g. cudagraph_mode)")
    p.add_argument("--additional-config", default="", help="JSON string for AscendConfig additional_config")
    p.add_argument("--quantization", default="", help="Quantization method (e.g. 'ascend' for W8A8)")
    p.add_argument("--extra-serve-args", nargs="*", default=[], help="Extra arguments for vLLM serve command")

    # Attach mode: profile a service already managed by vllm-ascend-serving
    p.add_argument("--attach", action="store_true",
                   help="Attach to a running service managed by the serving skill")
    p.add_argument("--baseline-from", default="",
                   help="Path to a previous run directory to reuse baseline npu-smi data (for attach mode)")
    p.add_argument("--resume-run", default="",
                   help="Path to a previous run directory to merge new data into (for two-phase attach)")
    return p.parse_args()


def collect_npu_smi(ep: SshEndpoint, label: str, local_path: Path) -> dict:
    """Run npu-smi info and save output, return parsed HBM data."""
    progress(f"Collecting npu-smi snapshot: {label}")
    r = ssh_exec(ep, f"{ENV_PREAMBLE} npu-smi info", timeout=30)
    (local_path / f"{label}_npu_smi.txt").write_text(r.stdout)

    hbm = {}
    lines = r.stdout.splitlines()
    current_npu = None
    for line in lines:
        stripped = line.strip().strip("|").strip()
        if not stripped or stripped.startswith("+") or stripped.startswith("="):
            continue
        parts = [p.strip() for p in stripped.split("|")]
        # Line with NPU ID and name (e.g., "0     910B4")
        col0 = parts[0].strip() if parts else ""
        tokens = col0.split()
        if len(tokens) >= 2 and tokens[0].isdigit() and any(c.isalpha() for c in tokens[1]):
            current_npu = int(tokens[0])
            continue
        # Line with HBM data: look for "XXXX / YYYYY" pattern in last column
        if current_npu is not None:
            hbm_match = re.search(r"(\d+)\s*/\s*(\d+)\s*$", stripped)
            if hbm_match:
                used = int(hbm_match.group(1))
                total = int(hbm_match.group(2))
                if total > 1000:  # HBM is typically > 1000 MB
                    hbm[current_npu] = {"used_mb": used, "total_mb": total}
                    current_npu = None
    return hbm


def build_serve_command(args: argparse.Namespace, python: str) -> str:
    """Build the vLLM serve command string."""
    parts = [
        python, "-m", "vllm.entrypoints.openai.api_server",
        "--model", args.model,
        "--tensor-parallel-size", str(args.tp),
        "--trust-remote-code",
        "--gpu-memory-utilization", str(args.gpu_memory_utilization),
        "--max-model-len", str(args.max_model_len),
        "--port", str(args.port),
    ]
    if args.dp > 1:
        parts.extend(["--data-parallel-size", str(args.dp)])
    if args.enable_expert_parallel:
        parts.append("--enable-expert-parallel")
    if args.enforce_eager:
        parts.append("--enforce-eager")
    if args.speculative_config:
        parts.extend(["--speculative-config", shlex.quote(args.speculative_config)])
    if args.compilation_config:
        parts.extend(["--compilation-config", shlex.quote(args.compilation_config)])
    if args.additional_config:
        parts.extend(["--additional-config", shlex.quote(args.additional_config)])
    if args.quantization:
        parts.extend(["--quantization", args.quantization])
    parts.extend(args.extra_serve_args)
    return " ".join(parts)


def _compute_devices(args: argparse.Namespace) -> str:
    """Compute ASCEND_RT_VISIBLE_DEVICES string."""
    if args.devices:
        return args.devices
    total = args.tp * args.dp
    return ",".join(str(i) for i in range(total))


def start_service_with_msprof(
    ep: SshEndpoint,
    args: argparse.Namespace,
    python: str,
    remote_dir: str,
) -> None:
    """Start vLLM serve wrapped by msprof for component-level memory data."""
    serve_cmd = build_serve_command(args, python)
    devices = _compute_devices(args)

    script_path = f"{remote_dir}/_serve.sh"
    script_content = (
        f"#!/bin/bash\n"
        f"{ENV_PREAMBLE}\n"
        f"export PATH=$(dirname {python}):$PATH\n"
        f"export ASCEND_RT_VISIBLE_DEVICES={devices}\n"
        f"exec {serve_cmd}\n"
    )
    ssh_write_text(ep, script_content, script_path)
    ssh_exec(ep, f"chmod +x {script_path}")

    msprof_cmd = (
        f"{ENV_PREAMBLE} "
        f"cd /tmp; "
        f"nohup msprof --output={remote_dir}/msprof_data "
        f"--sys-hardware-mem=on --sys-hardware-mem-freq={args.msprof_mem_freq} "
        f'--application="bash {script_path}" '
        f"> {remote_dir}/msprof_stdout.log 2>&1 & "
        f"echo $!"
    )
    r = ssh_exec(ep, msprof_cmd)
    progress(f"msprof started, PID hint: {r.stdout.strip()}")


def wait_for_health(ep: SshEndpoint, port: int, timeout: int) -> float:
    """Wait for vLLM service to become healthy. Returns elapsed seconds."""
    progress("Waiting for service health check...")
    t0 = time.time()
    for i in range(timeout):
        r = ssh_exec(ep, f"curl -sf -o /dev/null -w '%{{http_code}}' http://localhost:{port}/health 2>/dev/null || true", check=False, timeout=10)
        if "200" in r.stdout:
            elapsed = time.time() - t0
            progress(f"Service ready in {elapsed:.0f}s")
            return elapsed
        if i % 30 == 0 and i > 0:
            progress(f"Still waiting... ({i}s elapsed)")
        time.sleep(1)
    raise TimeoutError(f"Service not ready after {timeout}s")


def send_inference(
    ep: SshEndpoint,
    args: argparse.Namespace,
    *,
    model_name: str = "",
    port: int | None = None,
) -> dict:
    """Send inference request (text completion or multimodal chat).

    *model_name* overrides args.model for the API request body (useful when
    the serving skill sets --served-model-name to something different from the
    weight path).  *port* overrides args.port.
    """
    api_model = model_name or args.model
    api_port = port or args.port

    if args.image_url:
        progress("Sending multimodal (VL) inference request...")
        payload = json.dumps({
            "model": api_model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": args.image_url}},
                    {"type": "text", "text": args.prompt or "Describe this image in detail."},
                ],
            }],
            "max_tokens": args.max_tokens,
            "temperature": 0.7,
        })
        api_endpoint = f"http://localhost:{api_port}/v1/chat/completions"
    else:
        progress("Sending text inference request...")
        payload = json.dumps({
            "model": api_model,
            "prompt": args.prompt,
            "max_tokens": args.max_tokens,
            "temperature": 0.7,
        })
        api_endpoint = f"http://localhost:{api_port}/v1/completions"

    cmd = (
        f"curl -s {api_endpoint} "
        f'-H "Content-Type: application/json" '
        f"-d '{payload}'"
    )
    r = ssh_exec(ep, cmd, timeout=180)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"raw": r.stdout[:500]}


def stop_service(ep: SshEndpoint) -> None:
    """Kill all vLLM and msprof processes."""
    progress("Stopping service and msprof...")
    ssh_exec(ep, (
        "pkill -f 'vllm.entrypoints' 2>/dev/null; "
        "sleep 3; "
        "pkill -9 -f 'vllm.entrypoints' 2>/dev/null; "
        "sleep 2; "
        "true"
    ), check=False)


def collect_vllm_logs(ep: SshEndpoint, remote_dir: str, local_path: Path) -> str:
    """Fetch vLLM serve logs from remote."""
    for logname in ["msprof_stdout.log", "vllm_serve.log"]:
        r = ssh_exec(ep, f"cat {remote_dir}/{logname} 2>/dev/null", check=False)
        if r.stdout.strip():
            (local_path / "vllm_serve.log").write_text(r.stdout)
            return r.stdout
    return ""


def _discover_prof_device_map(
    ep: SshEndpoint,
    search_root: str,
) -> dict[str, list[int]]:
    """Map PROF directory names to device IDs they cover.

    Returns e.g. {"PROF_000001_...": [0,1,2,3], "PROF_000002_...": [4,5,6,7]}.
    Device IDs are discovered from ``device_*`` subdirectories within each PROF.
    """
    r = ssh_exec(
        ep,
        f"find {search_root} -maxdepth 1 -name 'PROF_*' -type d",
        check=False,
    )
    prof_dirs = [d.strip() for d in r.stdout.strip().splitlines() if d.strip()]
    mapping: dict[str, list[int]] = {}
    for pdir in prof_dirs:
        prof_name = Path(pdir).name
        r2 = ssh_exec(
            ep,
            f"find {shlex.quote(pdir)} -maxdepth 1 -name 'device_*' -type d "
            f"| sed 's|.*/device_||'",
            check=False,
        )
        devs = []
        for tok in r2.stdout.strip().splitlines():
            tok = tok.strip()
            if tok.isdigit():
                devs.append(int(tok))
        mapping[prof_name] = sorted(devs)
    return mapping


def collect_msprof_csvs(
    ep: SshEndpoint,
    remote_dir: str,
    local_path: Path,
    *,
    msprof_data_subdir: bool = True,
) -> dict:
    """Download key msprof CSV files and return a manifest.

    When *msprof_data_subdir* is True (default, standalone mode), CSVs are found
    under ``remote_dir/msprof_data/``.  When False (attach mode), *remote_dir*
    already points to the msprof data directory itself.

    The manifest maps ``local_filename`` → ``relative_path`` and additionally
    stores a ``__prof_device_map__`` key mapping each CSV to the device IDs
    of its parent PROF directory (for per-device attribution).
    """
    csv_dir = local_path / "msprof_csvs"
    csv_dir.mkdir(exist_ok=True)

    search_root = f"{remote_dir}/msprof_data" if msprof_data_subdir else remote_dir
    prof_device_map = _discover_prof_device_map(ep, search_root)

    r = ssh_exec(ep, f"find {search_root} -name '*.csv' -size +100c", check=False)
    csvs = [f.strip() for f in r.stdout.strip().splitlines() if f.strip()]
    manifest: dict[str, Any] = {}
    csv_device_map: dict[str, list[int]] = {}

    for remote_csv in csvs:
        basename = Path(remote_csv).name
        r2 = ssh_exec(ep, f"cat {shlex.quote(remote_csv)}", check=False)
        if r2.stdout.strip():
            local_csv = csv_dir / basename
            if local_csv.exists():
                stem = local_csv.stem
                suffix = local_csv.suffix
                idx = 1
                while local_csv.exists():
                    local_csv = csv_dir / f"{stem}_{idx}{suffix}"
                    idx += 1
            local_csv.write_text(r2.stdout)
            manifest[local_csv.name] = str(local_csv.relative_to(local_path))

            for prof_name, devs in prof_device_map.items():
                if prof_name in remote_csv:
                    csv_device_map[local_csv.name] = devs
                    break

    manifest["__prof_device_map__"] = csv_device_map
    return manifest


def collect_model_config(ep: SshEndpoint, model_path: str, local_path: Path) -> dict:
    """Fetch model config.json for theoretical weight calculation."""
    r = ssh_exec(ep, f"cat {model_path}/config.json 2>/dev/null", check=False)
    if r.stdout.strip():
        (local_path / "model_config.json").write_text(r.stdout)
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            pass
    return {}


def collect_weight_manifest(ep: SshEndpoint, python: str, model_path: str, local_path: Path) -> dict:
    """Run weight_inspector.py on remote to extract safetensors tensor metadata."""
    progress("Inspecting model weight files (safetensors headers)...")
    inspector_src = (Path(__file__).parent / "weight_inspector.py").read_text()

    remote_script = "/tmp/_vaws_weight_inspector.py"
    ssh_write_text(ep, inspector_src, remote_script)
    r = ssh_exec(ep, f"{python} {remote_script} {shlex.quote(model_path)}", check=False, timeout=120)

    if r.returncode != 0:
        progress(f"WARNING: weight inspector failed: {r.stderr[:500]}")
        return {}

    try:
        manifest = json.loads(r.stdout)
        (local_path / "weight_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False)
        )
        progress(f"Weight manifest: {manifest.get('total_tensors', 0)} tensors, "
                 f"{manifest.get('total_gib', 0)} GiB total")
        return manifest
    except json.JSONDecodeError:
        progress(f"WARNING: weight inspector output not valid JSON")
        return {}


def _resolve_attach_state(machine: dict, args: argparse.Namespace) -> dict:
    """Load and validate serving state for attach mode.

    Accepts 'ready', 'started', or 'stopped'.  When stopped, only msprof CSV
    collection and weight/config analysis are possible (no health check, no
    inference).
    """
    alias = get_machine_alias(machine)
    state = load_serving_state(alias)
    if state is None:
        raise SystemExit(
            f"No serving state found for machine '{alias}'. "
            "Start a service first with the vllm-ascend-serving skill, "
            "or run in standalone mode (without --attach)."
        )

    status = state.get("status", "unknown")
    if status not in ("ready", "started", "stopped"):
        raise SystemExit(
            f"Service on '{alias}' has status '{status}'. "
            "Expected 'ready', 'started', or 'stopped'. "
            "Start a service first with the vllm-ascend-serving skill."
        )

    return state


def _load_baseline_from(baseline_path: str, run_dir: Path) -> dict:
    """Reuse baseline npu-smi data from a previous profiling run."""
    src = Path(baseline_path) / "baseline_npu_smi.txt"
    if not src.exists():
        manifest_path = Path(baseline_path) / "manifest.json"
        if manifest_path.exists():
            m = json.loads(manifest_path.read_text())
            return m.get("baseline_hbm", {})
        return {}

    import shutil
    shutil.copy2(src, run_dir / "baseline_npu_smi.txt")

    manifest_path = Path(baseline_path) / "manifest.json"
    if manifest_path.exists():
        m = json.loads(manifest_path.read_text())
        return m.get("baseline_hbm", {})
    return {}


def _collect_serving_logs(ep: SshEndpoint, serving_state: dict, local_path: Path) -> str:
    """Fetch vLLM logs from the serving skill's runtime directory.

    Combines both stdout and stderr since critical memory info (weight load
    size, KV cache size, graph capture) is logged to stdout while warnings
    and progress bars go to stderr.
    """
    combined = []
    for key in ("log_stdout", "log_stderr"):
        remote_log = serving_state.get(key, "")
        if not remote_log:
            continue
        r = ssh_exec(ep, f"cat {shlex.quote(remote_log)} 2>/dev/null", check=False)
        if r.stdout.strip():
            combined.append(r.stdout)
    full_log = "\n".join(combined)
    if full_log.strip():
        (local_path / "vllm_serve.log").write_text(full_log)
    return full_log


def main() -> None:
    args = parse_args()
    machine = resolve_machine(args.machine)
    ep = endpoint_from_machine(machine)

    if args.attach:
        _main_attach(args, machine, ep)
    else:
        _main_standalone(args, machine, ep)


def _main_attach(
    args: argparse.Namespace,
    machine: dict,
    ep: SshEndpoint,
) -> None:
    """Attach mode: profile a service already managed by vllm-ascend-serving."""
    serving_state = _resolve_attach_state(machine, args)
    alias = get_machine_alias(machine)

    svc_model = serving_state.get("model", "")
    svc_port = serving_state.get("port", 8000)
    svc_tp = serving_state.get("tp")
    svc_dp = serving_state.get("dp")
    svc_devices = serving_state.get("devices", "")
    svc_extra_args = serving_state.get("extra_args", [])
    served_model_name = serving_state.get("served_model_name", "")

    model = args.model or svc_model
    if not model:
        raise SystemExit("Cannot determine model path. Provide --model or ensure serving state has it.")
    tp = args.tp if args.tp is not None else (svc_tp or 1)
    dp = args.dp if args.dp is not None else (svc_dp or 1)
    port = args.port if args.port is not None else svc_port
    devices = args.devices or svc_devices or ",".join(str(i) for i in range(tp * dp))

    svc_status = serving_state.get("status", "unknown")
    service_alive = svc_status in ("ready", "started")

    progress(f"Attaching to service on '{alias}' (port={port}, model={model})")
    progress(f"  tp={tp}, dp={dp}, devices={devices}, status={svc_status}")
    if served_model_name:
        progress(f"  served_model_name={served_model_name}")
    if not service_alive:
        progress("  Service is stopped — will only collect msprof CSVs, weight manifest, and config")

    _extract_serve_config_from_extra_args(args, svc_extra_args)

    model_tag = Path(model).name.replace("/", "_")
    if args.resume_run:
        run_dir = Path(args.resume_run)
        if not run_dir.exists():
            raise SystemExit(f"--resume-run directory does not exist: {run_dir}")
        progress(f"Resuming into existing run: {run_dir}")
    else:
        run_dir = ensure_run_dir(tag=args.tag or f"attach_{model_tag}")
    remote_dir = f"/tmp/vaws_memprof_{int(time.time())}"

    progress(f"Output directory: {run_dir}")
    ssh_exec(ep, f"mkdir -p {remote_dir}")

    python = find_python(ep)

    # Detect msprof: serving used our wrapper → msprof data at runtime_dir/msprof_data
    svc_wrap = serving_state.get("wrap_script", "")
    svc_runtime_dir = serving_state.get("runtime_dir", "")
    msprof_used = MSPROF_WRAPPER_REMOTE_PATH in svc_wrap
    msprof_data_dir = f"{svc_runtime_dir}/msprof_data" if msprof_used and svc_runtime_dir else ""
    if not msprof_used:
        progress(
            "WARNING: 服务未使用 msprof wrapper 启动，报告中将无法提供组件级内存拆分。"
            "建议使用 msprof wrapper 重新启动服务以获得完整的可追溯数据。"
        )

    manifest: dict = {
        "mode": "attach",
        "model": model,
        "tp": tp,
        "dp": dp,
        "devices": devices,
        "port": port,
        "served_model_name": served_model_name,
        "msprof_enabled": msprof_used,
        "msprof_output_dir": msprof_data_dir,
        "run_dir": str(run_dir),
        "serving_state_ref": f".vaws-local/serving/{alias}.json",
        "serving_runtime_dir": svc_runtime_dir,
        "speculative_config": args.speculative_config,
        "compilation_config": args.compilation_config,
        "additional_config": args.additional_config,
        "quantization": args.quantization,
        "enforce_eager": args.enforce_eager,
        "image_url": args.image_url,
        "service_alive": service_alive,
    }

    # Baseline: reuse from previous run or skip
    if args.baseline_from:
        progress(f"Reusing baseline from: {args.baseline_from}")
        manifest["baseline_hbm"] = _load_baseline_from(args.baseline_from, run_dir)
        manifest["baseline_source"] = args.baseline_from
    else:
        manifest["baseline_hbm"] = {}
        manifest["baseline_source"] = "unavailable"

    if service_alive:
        # Full collection: health check, npu-smi, logs, inference
        try:
            wait_for_health(ep, port, timeout=args.health_timeout)
        except TimeoutError:
            raise SystemExit(
                f"Service on port {port} is not responding to /health after "
                f"{args.health_timeout}s. Check service status with the serving skill."
            )

        manifest["after_ready_hbm"] = collect_npu_smi(ep, "after_ready", run_dir)
        _collect_serving_logs(ep, serving_state, run_dir)

        api_model = served_model_name or model
        manifest["inference_response"] = send_inference(
            ep, args, model_name=api_model, port=port,
        )
        manifest["after_infer_hbm"] = collect_npu_smi(ep, "after_infer", run_dir)
    else:
        # Service stopped — collect logs (may still exist on disk) but skip
        # health check, npu-smi, and inference
        manifest["after_ready_hbm"] = {}
        manifest["after_infer_hbm"] = {}
        _collect_serving_logs(ep, serving_state, run_dir)

    # Model config + weight manifest (always possible regardless of service state)
    manifest["model_config"] = collect_model_config(ep, model, run_dir)
    manifest["weight_manifest_collected"] = bool(
        collect_weight_manifest(ep, python, model, run_dir)
    )

    # Collect msprof CSVs if service was wrapped with msprof
    if msprof_used and msprof_data_dir:
        r = ssh_exec(ep, f"find {shlex.quote(msprof_data_dir)} -name '*.csv' -size +100c 2>/dev/null | head -1", check=False)
        if r.stdout.strip():
            progress("Collecting msprof CSVs from serving runtime...")
            manifest["msprof_csvs"] = collect_msprof_csvs(
                ep, msprof_data_dir, run_dir, msprof_data_subdir=False,
            )
        elif not service_alive:
            # Service stopped but CSVs not found → run export first
            progress("Running msprof export (service stopped, data not yet exported)...")
            run_msprof_export(ep, msprof_data_dir)
            manifest["msprof_csvs"] = collect_msprof_csvs(
                ep, msprof_data_dir, run_dir, msprof_data_subdir=False,
            )
        else:
            progress("msprof data will be available after service stop + export")
            manifest["msprof_csvs_pending"] = True

    if service_alive:
        progress("Attach-mode collection complete (service left running)")
    else:
        progress("Attach-mode collection complete (service was stopped, collected available data)")

    # When resuming, merge into the existing manifest so both phases
    # contribute to a single complete run.
    existing_manifest_path = run_dir / "manifest.json"
    if args.resume_run and existing_manifest_path.exists():
        existing = json.loads(existing_manifest_path.read_text())
        for key, val in manifest.items():
            if val in (None, {}, [], "", False, 0) and existing.get(key) not in (None, {}, [], "", False, 0):
                continue
            existing[key] = val
        manifest = existing

    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    progress(f"Data saved to {run_dir}")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


def _extract_serve_config_from_extra_args(
    args: argparse.Namespace,
    extra_args: list[str],
) -> None:
    """Populate args.speculative_config etc. from serving state's extra_args
    if not already set via CLI.  This makes the manifest and analysis accurate
    when the user only specified --attach without repeating every flag."""
    flag_map = {
        "--speculative-config": "speculative_config",
        "-sc": "speculative_config",
        "--compilation-config": "compilation_config",
        "--additional-config": "additional_config",
        "--quantization": "quantization",
        "--gpu-memory-utilization": "gpu_memory_utilization",
        "--max-model-len": "max_model_len",
        "--enable-expert-parallel": "enable_expert_parallel",
        "--enforce-eager": "enforce_eager",
    }
    i = 0
    while i < len(extra_args):
        token = extra_args[i]
        attr = flag_map.get(token)
        if attr is None:
            i += 1
            continue
        if token in ("--enable-expert-parallel", "--enforce-eager"):
            if not getattr(args, attr, False):
                setattr(args, attr, True)
            i += 1
        elif i + 1 < len(extra_args):
            current = getattr(args, attr, "")
            if not current or current == "" or (isinstance(current, float) and attr == "gpu_memory_utilization"):
                val = extra_args[i + 1]
                field_type = type(getattr(args, attr))
                if field_type == float:
                    setattr(args, attr, float(val))
                elif field_type == int:
                    setattr(args, attr, int(val))
                else:
                    setattr(args, attr, val)
            i += 2
        else:
            i += 1


def _main_standalone(
    args: argparse.Namespace,
    machine: dict,
    ep: SshEndpoint,
) -> None:
    """Standalone mode: start service, profile, stop."""
    if not args.model:
        raise SystemExit("--model is required in standalone mode")
    tp = args.tp if args.tp is not None else 1
    dp = args.dp if args.dp is not None else 1
    port = args.port if args.port is not None else 8901
    args.tp = tp
    args.dp = dp
    args.port = port

    model_tag = Path(args.model).name.replace("/", "_")
    run_dir = ensure_run_dir(tag=args.tag or model_tag)
    remote_dir = f"/tmp/vaws_memprof_{int(time.time())}"

    progress(f"Output directory: {run_dir}")
    ssh_exec(ep, f"mkdir -p {remote_dir}")

    python = find_python(ep)
    progress(f"Python: {python}")

    # Pre-flight: verify msprof is available
    check_msprof_available(ep)

    manifest: dict = {
        "mode": "standalone",
        "model": args.model,
        "tp": tp,
        "dp": dp,
        "devices": _compute_devices(args),
        "gpu_memory_utilization": args.gpu_memory_utilization,
        "max_model_len": args.max_model_len,
        "msprof_enabled": True,
        "run_dir": str(run_dir),
        "speculative_config": args.speculative_config,
        "compilation_config": args.compilation_config,
        "additional_config": args.additional_config,
        "quantization": args.quantization,
        "enforce_eager": args.enforce_eager,
        "image_url": args.image_url,
    }

    # Phase 0: baseline
    manifest["baseline_hbm"] = collect_npu_smi(ep, "baseline", run_dir)

    # Phase 1: start service (always with msprof for traceable memory data)
    start_service_with_msprof(ep, args, python, remote_dir)

    try:
        manifest["startup_seconds"] = wait_for_health(ep, port, args.health_timeout)
    except TimeoutError as e:
        progress(f"ERROR: {e}")
        collect_vllm_logs(ep, remote_dir, run_dir)
        stop_service(ep)
        manifest["error"] = str(e)
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        sys.exit(1)

    # Phase 2: after ready
    manifest["after_ready_hbm"] = collect_npu_smi(ep, "after_ready", run_dir)
    collect_vllm_logs(ep, remote_dir, run_dir)

    # Phase 3: inference
    manifest["inference_response"] = send_inference(ep, args, port=port)
    manifest["after_infer_hbm"] = collect_npu_smi(ep, "after_infer", run_dir)

    # Phase 4: stop service
    stop_service(ep)
    time.sleep(5)

    # Phase 5: msprof export (via shared helper)
    run_msprof_export(ep, f"{remote_dir}/msprof_data")
    manifest["msprof_csvs"] = collect_msprof_csvs(ep, remote_dir, run_dir)

    # Phase 6: model config + weight manifest
    manifest["model_config"] = collect_model_config(ep, args.model, run_dir)
    manifest["weight_manifest_collected"] = bool(
        collect_weight_manifest(ep, python, args.model, run_dir)
    )

    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    progress(f"Collection complete. Data saved to {run_dir}")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
