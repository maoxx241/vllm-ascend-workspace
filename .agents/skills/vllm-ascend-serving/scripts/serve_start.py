#!/usr/bin/env python3
"""Start a vllm-ascend online service on a workspace-managed remote container.

Usage examples:

    # Fresh start with explicit params
    python3 serve_start.py --machine blue-a --model /data/models/Qwen3-32B \\
        --tp 4 --devices 0,1,2,3 -- --max-model-len 4096

    # Relaunch with same config
    python3 serve_start.py --machine blue-a --relaunch

    # Relaunch with a new env variable
    python3 serve_start.py --machine blue-a --relaunch --extra-env VLLM_USE_V1=1

    # Relaunch and remove an old env
    python3 serve_start.py --machine blue-a --relaunch --unset-env MY_DEBUG

Progress on stderr as __VAWS_SERVING_PROGRESS__=<json>.
Final result on stdout as a single JSON object.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import (
    ROOT,
    SshEndpoint,
    container_endpoint,
    emit_progress,
    host_endpoint,
    load_serving_state,
    now_utc,
    print_json,
    probe_npus,
    resolve_machine,
    save_serving_state,
    select_devices,
    ssh_exec,
)

RUNTIME_DIR_BASE = ".vaws-runtime/serving"
DEFAULT_HEALTH_TIMEOUT = 300
HEALTH_POLL_INTERVAL = 5


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------

def run_parity(machine: str) -> dict[str, Any]:
    parity_script = ROOT / ".agents" / "skills" / "remote-code-parity" / "scripts" / "parity_sync.py"
    cmd = [sys.executable, str(parity_script), "--machine", machine]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    for line in (result.stderr or "").splitlines():
        if line.startswith("__VAWS_PARITY_PROGRESS__="):
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
    if result.returncode != 0:
        return {
            "status": "failed",
            "error": f"parity sync failed (rc={result.returncode})",
            "stderr_tail": (result.stderr or "")[-1000:],
        }
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "status": "failed",
            "error": "parity sync returned non-JSON output",
            "stdout_tail": (result.stdout or "")[-500:],
        }


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------

def find_free_port(ep: SshEndpoint) -> int:
    script = (
        "python3 -c \"\n"
        "import socket, random, json\n"
        "for _ in range(50):\n"
        "    port = random.randint(30000, 60000)\n"
        "    try:\n"
        "        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "        s.bind(('0.0.0.0', port))\n"
        "        s.close()\n"
        "        print(json.dumps({'port': port}))\n"
        "        exit(0)\n"
        "    except OSError:\n"
        "        continue\n"
        "print(json.dumps({'error': 'no free port found'}))\n"
        "exit(1)\n"
        "\""
    )
    result = ssh_exec(ep, script, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"port discovery failed: {result.stderr[:500]}")
    data = json.loads(result.stdout.strip())
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["port"]


# ---------------------------------------------------------------------------
# Launch script builder (the core escaping-safe layer)
# ---------------------------------------------------------------------------

def build_launch_script(
    *,
    runtime_dir: str,
    model: str,
    served_model_name: str,
    port: int,
    tp: int | None,
    dp: int | None,
    devices: str | None,
    extra_env: dict[str, str],
    extra_args: list[str],
) -> str:
    lines: list[str] = ["set -e"]

    lines.append(f"mkdir -p {shlex.quote(runtime_dir)}")

    # Ascend environment — source the managed profile that sets PATH,
    # LD_LIBRARY_PATH, CANN, ATB, and the correct Python.
    lines.append(
        "if [ -f /etc/profile.d/vaws-ascend-env.sh ]; then"
        "  set +u; source /etc/profile.d/vaws-ascend-env.sh; set -u;"
        " fi"
    )
    lines.append(
        'export LD_LIBRARY_PATH='
        '"/usr/local/Ascend/driver/lib64/driver'
        ':/usr/local/Ascend/driver/lib64'
        '${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"'
    )

    # vllm-ascend custom CANN operators (aclnnAddRmsNormBias etc.)
    # Locate set_env.bash dynamically — vendor name may change across versions.
    lines.append(
        '_CUST_BASE=$(python3 -c '
        '"import vllm_ascend,os;print(os.path.join(os.path.dirname(vllm_ascend.__file__),'
        '\'_cann_ops_custom\'))" 2>/dev/null || true)'
    )
    lines.append(
        'if [ -n "$_CUST_BASE" ] && [ -d "$_CUST_BASE" ]; then'
        '  _CUST_ENV=$(find "$_CUST_BASE" -name set_env.bash -path "*/bin/set_env.bash" 2>/dev/null | head -1);'
        '  if [ -n "$_CUST_ENV" ]; then set +u; source "$_CUST_ENV"; set -u; fi;'
        " fi"
    )

    if devices:
        lines.append(f"export ASCEND_RT_VISIBLE_DEVICES={shlex.quote(devices)}")

    for key, value in extra_env.items():
        lines.append(f"export {key}={shlex.quote(value)}")

    # Launch from the runtime dir — NOT from /vllm-workspace, which would
    # shadow the installed vllm package with the source tree.
    lines.append(f"cd {shlex.quote(runtime_dir)}")

    # Build argv — every token individually quoted for bash safety
    argv_tokens = ["vllm", "serve", shlex.quote(model)]
    argv_tokens.extend(["--host", "0.0.0.0"])
    argv_tokens.extend(["--port", str(port)])
    if served_model_name:
        argv_tokens.extend(["--served-model-name", shlex.quote(served_model_name)])
    if tp is not None:
        argv_tokens.extend(["--tensor-parallel-size", str(tp)])
    if dp is not None:
        argv_tokens.extend(["--data-parallel-size", str(dp)])
    for arg in extra_args:
        argv_tokens.append(shlex.quote(arg))

    cmd_str = " ".join(argv_tokens)
    stdout_log = f"{runtime_dir}/stdout.log"
    stderr_log = f"{runtime_dir}/stderr.log"
    pid_file = f"{runtime_dir}/pid"

    lines.append(
        f"nohup {cmd_str}"
        f" > {shlex.quote(stdout_log)}"
        f" 2> {shlex.quote(stderr_log)}"
        f" </dev/null &"
    )
    lines.append("_PID=$!")
    lines.append("disown $_PID")
    lines.append(f"echo $_PID > {shlex.quote(pid_file)}")
    lines.append("echo $_PID")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def check_alive(ep: SshEndpoint, pid: int) -> bool:
    r = ssh_exec(ep, f"kill -0 {pid} 2>/dev/null && echo alive || echo dead", check=False)
    return r.stdout.strip() == "alive"


def check_health(ep: SshEndpoint, port: int) -> bool:
    script = f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 3 http://127.0.0.1:{port}/health 2>/dev/null || echo 000"
    r = ssh_exec(ep, script, check=False)
    return r.stdout.strip() == "200"


def check_models(ep: SshEndpoint, port: int) -> dict[str, Any] | None:
    script = f"curl -s --connect-timeout 3 http://127.0.0.1:{port}/v1/models 2>/dev/null || true"
    r = ssh_exec(ep, script, check=False)
    text = r.stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if data.get("data"):
            return data
    except json.JSONDecodeError:
        pass
    return None


def read_remote_tail(ep: SshEndpoint, remote_path: str, lines: int = 30) -> str:
    r = ssh_exec(ep, f"tail -{lines} {shlex.quote(remote_path)} 2>/dev/null || echo '(no log)'", check=False)
    return r.stdout.strip()


def wait_for_ready(
    ep: SshEndpoint,
    pid: int,
    port: int,
    runtime_dir: str,
    timeout: int,
) -> dict[str, Any]:
    start = time.monotonic()
    deadline = start + timeout
    health_ok = False
    models_ok = False

    while time.monotonic() < deadline:
        if not check_alive(ep, pid):
            stderr_tail = read_remote_tail(ep, f"{runtime_dir}/stderr.log")
            return {
                "ready": False,
                "alive": False,
                "error": "process exited before becoming ready",
                "stderr_tail": stderr_tail,
                "elapsed_seconds": round(time.monotonic() - start, 1),
            }

        if not health_ok:
            health_ok = check_health(ep, port)
            if health_ok:
                emit_progress("probe-health", "/health returned 200")

        if health_ok and not models_ok:
            if check_models(ep, port) is not None:
                models_ok = True
                emit_progress("probe-models", "/v1/models returned model list")

        if health_ok and models_ok:
            return {
                "ready": True,
                "alive": True,
                "elapsed_seconds": round(time.monotonic() - start, 1),
            }

        time.sleep(HEALTH_POLL_INTERVAL)

    return {
        "ready": False,
        "alive": check_alive(ep, pid),
        "health": health_ok,
        "models": models_ok,
        "error": f"timed out after {timeout}s waiting for service",
        "elapsed_seconds": round(time.monotonic() - start, 1),
    }


# ---------------------------------------------------------------------------
# Relaunch merge
# ---------------------------------------------------------------------------

def merge_with_previous(
    previous: dict[str, Any],
    *,
    model: str | None,
    served_model_name: str | None,
    tp: int | None,
    dp: int | None,
    devices: str | None,
    extra_env: dict[str, str],
    unset_env: list[str],
    extra_args: list[str],
    unset_args: list[str],
) -> dict[str, Any]:
    merged = dict(previous)
    if model is not None:
        merged["model"] = model
    if served_model_name is not None:
        merged["served_model_name"] = served_model_name
    if tp is not None:
        merged["tp"] = tp
    if dp is not None:
        merged["dp"] = dp
    if devices is not None:
        merged["devices"] = devices

    prev_env = dict(merged.get("env", {}))
    for key in unset_env:
        prev_env.pop(key, None)
    prev_env.update(extra_env)
    merged["env"] = prev_env

    prev_args = list(merged.get("extra_args", []))
    if unset_args:
        cleaned: list[str] = []
        i = 0
        while i < len(prev_args):
            arg = prev_args[i]
            if any(arg.startswith(u) for u in unset_args):
                if "=" not in arg:
                    nxt = prev_args[i + 1] if i + 1 < len(prev_args) else None
                    if nxt is not None and not nxt.startswith("-"):
                        i += 1
                i += 1
                continue
            cleaned.append(arg)
            i += 1
        prev_args = cleaned
    prev_args.extend(extra_args)
    merged["extra_args"] = prev_args

    return merged


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    p.add_argument("--machine", required=True, help="machine alias or host IP")
    p.add_argument("--model", help="absolute model weight path on the remote container")
    p.add_argument(
        "--served-model-name", "--served-name",
        dest="served_model_name",
        help="model name exposed via /v1/models (default: directory basename of --model)",
    )
    p.add_argument("--tp", "--tensor-parallel-size", dest="tp", type=int)
    p.add_argument("--dp", "--data-parallel-size", dest="dp", type=int)
    p.add_argument("--devices", help="ASCEND_RT_VISIBLE_DEVICES, e.g. 0,1,2,3")
    p.add_argument(
        "--extra-env", action="append", default=[],
        help="KEY=VALUE (repeatable)",
    )
    p.add_argument(
        "--unset-env", action="append", default=[],
        help="remove an env var from inherited config (repeatable)",
    )
    p.add_argument(
        "--unset-args", action="append", default=[],
        help="remove a vllm arg prefix from inherited config (repeatable)",
    )
    p.add_argument("--relaunch", action="store_true", help="reuse previous config as base")
    p.add_argument("--skip-parity", action="store_true", help="skip remote-code-parity gate")
    p.add_argument("--port", type=int, help="force a specific port")
    p.add_argument(
        "--health-timeout", type=int, default=DEFAULT_HEALTH_TIMEOUT,
        help=f"seconds to wait for /health + /v1/models (default: {DEFAULT_HEALTH_TIMEOUT})",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Split on bare -- to separate our args from vllm passthrough args
    own_argv: list[str] = argv
    vllm_extra: list[str] = []
    if "--" in argv:
        idx = argv.index("--")
        own_argv = argv[:idx]
        vllm_extra = argv[idx + 1:]

    args = build_parser().parse_args(own_argv)

    try:
        # ---- resolve machine ----
        emit_progress("resolve-machine", f"looking up {args.machine}")
        record = resolve_machine(args.machine)
        alias = record["alias"]
        ep = container_endpoint(record)
        runtime_base = record["container"].get("workdir", "/vllm-workspace")

        # ---- parse env overrides ----
        extra_env: dict[str, str] = {}
        for item in args.extra_env:
            if "=" not in item:
                print_json({"status": "failed", "error": f"bad --extra-env {item!r}, expected KEY=VALUE"})
                return 1
            k, _, v = item.partition("=")
            extra_env[k.strip()] = v

        # ---- resolve launch params (fresh or relaunch) ----
        if args.relaunch:
            previous = load_serving_state(alias)
            if previous is None:
                print_json({
                    "status": "failed",
                    "error": f"no previous launch state for {alias}; cannot --relaunch without a prior start",
                    "machine": alias,
                })
                return 1
            merged = merge_with_previous(
                previous,
                model=args.model,
                served_model_name=args.served_model_name,
                tp=args.tp, dp=args.dp, devices=args.devices,
                extra_env=extra_env, unset_env=args.unset_env,
                extra_args=vllm_extra, unset_args=args.unset_args,
            )
            model = merged["model"]
            served_model_name = merged["served_model_name"]
            tp = merged.get("tp")
            dp = merged.get("dp")
            devices = merged.get("devices")
            launch_env = merged.get("env", {})
            launch_extra_args = merged.get("extra_args", [])
            emit_progress("resolve-params", "merged delta onto previous config", relaunch=True)
        else:
            if not args.model:
                print_json({
                    "status": "needs_input",
                    "error": "--model is required for a fresh start",
                    "machine": alias,
                })
                return 1
            model = args.model
            served_model_name = args.served_model_name or Path(model).name
            tp = args.tp
            dp = args.dp
            devices = args.devices
            launch_env = extra_env
            launch_extra_args = vllm_extra

        # ---- stop existing service on this machine ----
        prev_state = load_serving_state(alias)
        if prev_state and prev_state.get("pid"):
            old_pid = prev_state["pid"]
            emit_progress("stop-existing", f"stopping previous service (pid={old_pid})")
            ssh_exec(
                ep,
                f"kill -2 {old_pid} 2>/dev/null || true; sleep 2; kill -15 {old_pid} 2>/dev/null || true",
                check=False,
            )
            time.sleep(1)

        # ---- parity gate ----
        if not args.skip_parity:
            emit_progress("parity-sync", "ensuring remote code parity")
            parity = run_parity(args.machine)
            parity_status = parity.get("status")
            if parity_status not in ("ready", "ok", "success"):
                print_json({
                    "status": "blocked",
                    "error": "remote-code-parity did not return ready",
                    "parity": parity,
                    "machine": alias,
                })
                return 1
            emit_progress("parity-sync", "parity confirmed")
        else:
            parity = {"status": "skipped"}

        # ---- probe NPUs on the HOST for cross-container visibility ----
        h_ep = host_endpoint(record)
        emit_progress("probe-npus", "checking NPU device availability (host)")
        try:
            npu_info = probe_npus(h_ep)
        except RuntimeError as exc:
            npu_info = None
            emit_progress("probe-npus", f"NPU probe failed (non-fatal): {exc}")

        if npu_info is not None:
            resolved_devices, device_error = select_devices(
                npu_info, requested_devices=devices, tp=tp,
            )
            if device_error:
                print_json({
                    "status": "needs_input",
                    "error": device_error,
                    "npu_info": npu_info,
                    "machine": alias,
                })
                return 1
            if resolved_devices is not None:
                devices = resolved_devices
                emit_progress(
                    "probe-npus",
                    f"using devices: {devices}",
                    free=npu_info.get("free"),
                    busy=list(npu_info.get("busy", {}).keys()),
                )

        # ---- validate model path exists remotely ----
        emit_progress("validate", f"checking model path: {model}")
        r = ssh_exec(ep, f"test -d {shlex.quote(model)} || test -f {shlex.quote(model)}", check=False)
        if r.returncode != 0:
            print_json({
                "status": "needs_input",
                "error": f"model path not found on remote container: {model}",
                "machine": alias,
            })
            return 1

        # ---- port ----
        if args.port:
            port = args.port
        else:
            emit_progress("allocate-port", "finding free port")
            port = find_free_port(ep)
        emit_progress("allocate-port", f"port {port}", port=port)

        # ---- launch ----
        instance_ts = now_utc().replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
        runtime_dir = f"{runtime_base}/{RUNTIME_DIR_BASE}/{instance_ts}"

        emit_progress("launch", "starting vllm serve")
        script = build_launch_script(
            runtime_dir=runtime_dir,
            model=model,
            served_model_name=served_model_name,
            port=port,
            tp=tp, dp=dp,
            devices=devices,
            extra_env=launch_env,
            extra_args=launch_extra_args,
        )
        result = ssh_exec(ep, script, check=False)
        if result.returncode != 0:
            print_json({
                "status": "failed",
                "error": "launch script failed",
                "stderr_tail": result.stderr[-1000:],
                "stdout_tail": result.stdout[-500:],
                "machine": alias,
            })
            return 1

        pid_line = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        try:
            pid = int(pid_line)
        except ValueError:
            print_json({
                "status": "failed",
                "error": f"cannot parse PID from launch output: {pid_line!r}",
                "machine": alias,
            })
            return 1

        emit_progress("launch", f"process started pid={pid}", pid=pid)

        # ---- probe readiness ----
        emit_progress("probe", f"waiting for ready (timeout={args.health_timeout}s)")
        readiness = wait_for_ready(ep, pid, port, runtime_dir, timeout=args.health_timeout)

        # ---- persist state (always, even if not ready — so stop can clean up) ----
        state = {
            "model": model,
            "served_model_name": served_model_name,
            "tp": tp,
            "dp": dp,
            "devices": devices,
            "env": launch_env,
            "extra_args": launch_extra_args,
            "machine": alias,
            "pid": pid,
            "port": port,
            "base_url": f"http://{ep.host}:{port}",
            "runtime_dir": runtime_dir,
            "log_stdout": f"{runtime_dir}/stdout.log",
            "log_stderr": f"{runtime_dir}/stderr.log",
            "started_at": now_utc(),
            "status": "ready" if readiness["ready"] else "started",
        }
        save_serving_state(alias, state)

        # ---- build output ----
        output: dict[str, Any] = {
            "status": "ready" if readiness["ready"] else "failed",
            "machine": alias,
            "base_url": f"http://{ep.host}:{port}",
            "container_ip": ep.host,
            "port": port,
            "pid": pid,
            "served_model_name": served_model_name,
            "model": model,
            "devices": devices,
            "tp": tp,
            "dp": dp,
            "log_stdout": f"{runtime_dir}/stdout.log",
            "log_stderr": f"{runtime_dir}/stderr.log",
            "runtime_dir": runtime_dir,
            "readiness": readiness,
            "parity_status": parity.get("status"),
        }
        if launch_env:
            output["env"] = launch_env
        if launch_extra_args:
            output["extra_args"] = launch_extra_args
        if not readiness["ready"]:
            output["stderr_tail"] = read_remote_tail(ep, f"{runtime_dir}/stderr.log")

        print_json(output)
        return 0 if readiness["ready"] else 1

    except Exception as exc:
        print_json({
            "status": "failed",
            "error": str(exc),
            "machine": getattr(args, "machine", None),
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
