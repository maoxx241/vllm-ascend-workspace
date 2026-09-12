#!/usr/bin/env python3
"""Start a vllm-ascend service through one coordinator TaskClient.run call."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import uuid
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

if __name__ == "__main__":
    from vaws_managed_entry import ensure_managed_entry
    ensure_managed_entry(repo_root=ROOT, entry_file=__file__, local_options=("--wrap-script-local",))

ensure_workspace_interpreter(repo_root=ROOT)

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _serving_common import (  # noqa: E402
    SERVICE_NAME,
    SshEndpoint,
    emit_progress,
    endpoint_from_reply,
    load_preset,
    now_utc,
    parse_devices_csv,
    print_json,
    service_port_of,
    ssh_exec,
)
from vaws_session_state import load_serving_state, save_serving_state  # noqa: E402
from vaws_coordinator.presentation import execution_summary
from vaws_task_target import (  # noqa: E402
    DONE,
    PENDING,
    RUNNING,
    named_environment,
    reject_reserved_env,
    run_command,
    service_resources,
    task_client,
    task_id_of,
)
from vaws_validate import require_env_name  # noqa: E402

DEFAULT_HEALTH_TIMEOUT = 300
HEALTH_POLL_INTERVAL = 5
_JSON_VALUE_FLAGS = (
    "--additional-config",
    "--model-loader-extra-config",
    "--speculative-config",
    "--compilation-config",
)
_ENV_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("Failed to infer device type", "device-type"),
    ("No module named 'vllm_ascend'", "missing-vllm-ascend"),
    ("No module named 'vllm'", "missing-vllm"),
    ("No module named 'torch_npu'", "missing-torch-npu"),
    ("cannot open shared object file", "missing-so"),
    ("libhccl.so", "missing-so"),
    ("RuntimeError:.*torch_npu", "torch-npu-error"),
    ("ImportError", "import-error"),
    ("ModuleNotFoundError", "module-not-found"),
]
_STAGE_MARKERS: list[tuple[str, str]] = [
    ("uvicorn running", "http-up"),
    ("application startup complete", "http-up"),
    ("capturing", "graph-capture"),
    ("graph capture", "graph-capture"),
    ("aclgraph", "graph-capture"),
    ("acl graph", "graph-capture"),
    ("torch.compile", "compile"),
    ("loading weights", "weight-load"),
    ("loading safetensors", "weight-load"),
    ("model loading", "weight-load"),
]


def _require_token(value: str, label: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} must not contain newline characters")
    return value


def local_preset_problems(preset: dict[str, Any] | None, extra_args: list[str]) -> list[str]:
    problems: list[str] = []
    if preset is None:
        return problems
    for flag in _JSON_VALUE_FLAGS:
        for idx, arg in enumerate(extra_args):
            if arg == flag:
                if idx + 1 >= len(extra_args):
                    problems.append(f"{flag} has no value")
                    continue
                value = extra_args[idx + 1]
            elif arg.startswith(f"{flag}="):
                value = arg[len(flag) + 1 :]
            else:
                continue
            try:
                json.loads(value)
            except json.JSONDecodeError as exc:
                problems.append(f"{flag} value is not valid JSON: {exc}")
    return problems


def build_serve_command(
    *,
    model: str,
    served_model_name: str,
    tp: int | None,
    dp: int | None,
    extra_args: list[str],
    wrap_script: str = "",
    wrap_script_content: str = "",
    runtime_dir: str = "",
    expected_vllm: str = "",
) -> str:
    argv = [
        '"$VAWS_PYTHON"',
        "-m",
        "vllm.entrypoints.cli.main",
        "serve",
        shlex.quote(_require_token(model, "--model")),
        "--host",
        "0.0.0.0",
        "--port",
        '"$VAWS_SERVICE_PORT"',
    ]
    if served_model_name:
        argv.extend([
            "--served-model-name",
            shlex.quote(_require_token(served_model_name, "--served-model-name")),
        ])
    if tp is not None:
        argv.extend(["--tensor-parallel-size", str(tp)])
    if dp is not None:
        argv.extend(["--data-parallel-size", str(dp)])
    for arg in extra_args:
        argv.append(shlex.quote(_require_token(arg, "extra vllm arg")))
    cmd_str = " ".join(argv)
    lines = [
        "set -e",
        'if [ -z "${VAWS_PYTHON:-}" ]; then echo "VAWS_PYTHON is unset; coordinator must inject the selected interpreter" >&2; exit 1; fi',
        'if [ -z "${VAWS_SERVICE_PORT:-}" ]; then echo "VAWS_SERVICE_PORT is unset; coordinator must inject the service port" >&2; exit 1; fi',
    ]
    if expected_vllm:
        lines.append(
            f'actual=$("$VAWS_PYTHON" -c "import vllm,sys;print(getattr(vllm,\'__version__\',\'\'))" 2>/dev/null || true)'
        )
        lines.append(
            f'if [ -n "$actual" ] && [ "$actual" != {shlex.quote(expected_vllm)} ]; then '
            f'echo "preset expects vllm {expected_vllm}, selected interpreter has $actual" >&2; exit 1; fi'
        )
    if wrap_script or wrap_script_content:
        if runtime_dir:
            lines.extend([f"runtime_dir={shlex.quote(runtime_dir)}", 'mkdir -m 700 -- "$runtime_dir"'])
        else:
            lines.append("runtime_dir=$(mktemp -d /tmp/vaws-serve.XXXXXX)")
        lines.append("cat > \"$runtime_dir/_serve.sh\" << 'VAWS_SERVE_EOF'")
        lines.append("#!/bin/bash")
        lines.append(f'if [ -z "${{VAWS_PYTHON:-}}" ]; then echo "VAWS_PYTHON is unset" >&2; exit 1; fi')
        lines.append(f"exec {cmd_str}")
        lines.append("VAWS_SERVE_EOF")
        lines.append("chmod +x \"$runtime_dir/_serve.sh\"")
        if wrap_script_content:
            delimiter = "VAWS_WRAPPER_" + uuid.uuid4().hex
            lines.extend([f'cat > "$runtime_dir/_wrap.sh" << \'{delimiter}\'', wrap_script_content,
                          delimiter, 'exec bash "$runtime_dir/_wrap.sh" "$runtime_dir/_serve.sh" "$runtime_dir"'])
        else:
            lines.append(f"exec bash {shlex.quote(wrap_script)} \"$runtime_dir/_serve.sh\" \"$runtime_dir\"")
    else:
        lines.append(f"exec {cmd_str}")
    return "\n".join(lines)


def classify_stage(text: str) -> str | None:
    lowered = text.lower()
    for needle, stage in _STAGE_MARKERS:
        if needle in lowered:
            return stage
    return None


def probe_ready_once(ep: SshEndpoint, port: int, *, log_text: str = "") -> dict[str, Any]:
    script = (
        f"code=$(curl --noproxy '*' -s -o /dev/null -w '%{{http_code}}' --connect-timeout 3 --max-time 5 "
        f"http://127.0.0.1:{port}/health 2>/dev/null || echo 000); "
        'echo "__HEALTH__=$code"; '
        'if [ "$code" = "200" ]; then echo __MODELS_BEGIN__; '
        f"curl --noproxy '*' -s --connect-timeout 3 --max-time 5 http://127.0.0.1:{port}/v1/models 2>/dev/null; "
        "echo; echo __MODELS_END__; fi"
    )
    result = ssh_exec(ep, script, check=False)
    out = result.stdout or ""
    models = None
    if "__MODELS_BEGIN__" in out and "__MODELS_END__" in out:
        body = out.split("__MODELS_BEGIN__", 1)[1].split("__MODELS_END__", 1)[0].strip()
        try:
            data = json.loads(body)
            if data.get("data"):
                models = data
        except json.JSONDecodeError:
            models = None
    return {
        "health": "__HEALTH__=200" in out,
        "models": models,
        "stage": classify_stage(log_text),
        "probe_error": result.returncode != 0,
    }


def probe_first_token(ep: SshEndpoint, port: int, served_model: str) -> dict[str, Any]:
    payload = json.dumps({"model": served_model, "prompt": "Hello", "max_tokens": 8, "temperature": 0})
    script = (
        "tmp=/tmp/vaws_first_token.$$.json; "
        f"code=$(curl -s -o $tmp -w '%{{http_code}}' --connect-timeout 3 --max-time 120 "
        f"-X POST http://127.0.0.1:{port}/v1/completions -H 'Content-Type: application/json' "
        f"-d {shlex.quote(payload)} 2>/dev/null || echo 000); "
        "echo __CODE__=$code; head -c 400 $tmp 2>/dev/null; rm -f $tmp"
    )
    result = ssh_exec(ep, script, check=False)
    out = result.stdout or ""
    return {"ok": "__CODE__=200" in out, "probe_error": result.returncode != 0, "detail": out[-300:]}


def wait_for_ready(ep: SshEndpoint, port: int, timeout: int, served_model: str, *, still_running, log_text) -> dict[str, Any]:
    start = time.monotonic()
    deadline = start + timeout
    health_ok = models_ok = token_ok = False
    phases: list[dict[str, Any]] = []

    def mark(stage: str) -> None:
        if not phases or phases[-1]["phase"] != stage:
            phases.append({"phase": stage, "at_seconds": round(time.monotonic() - start, 1)})
            emit_progress("probe", f"phase: {stage}")

    while time.monotonic() < deadline:
        if not still_running():
            return {
                "ready": False,
                "running": False,
                "error": "execution exited before becoming ready",
                "phases": phases,
                "elapsed_seconds": round(time.monotonic() - start, 1),
            }
        probe = probe_ready_once(ep, port, log_text=log_text())
        if probe.get("probe_error"):
            time.sleep(HEALTH_POLL_INTERVAL)
            continue
        if probe.get("stage"):
            mark(probe["stage"])
        if probe["health"] and not health_ok:
            health_ok = True
            mark("health-ok")
        if health_ok and probe["models"] is not None and not models_ok:
            models_ok = True
            mark("models-ok")
        if models_ok and not token_ok:
            token = probe_first_token(ep, port, served_model)
            if token.get("probe_error"):
                time.sleep(HEALTH_POLL_INTERVAL)
                continue
            if token["ok"]:
                token_ok = True
                mark("first-token-ok")
            else:
                mark("first-token-failing")
        if health_ok and models_ok and token_ok:
            return {"ready": True, "running": True, "phases": phases, "elapsed_seconds": round(time.monotonic() - start, 1)}
        time.sleep(HEALTH_POLL_INTERVAL)
    return {
        "ready": False,
        "running": still_running(),
        "health": health_ok,
        "models": models_ok,
        "first_token": token_ok,
        "error": f"timed out after {timeout}s waiting for service",
        "phases": phases,
        "elapsed_seconds": round(time.monotonic() - start, 1),
    }


def diagnose_env_failure(stderr_tail: str) -> dict[str, Any] | None:
    if not stderr_tail:
        return None
    matched = [tag for pattern, tag in _ENV_ERROR_PATTERNS if pattern in stderr_tail or re.search(pattern, stderr_tail)]
    if not matched:
        return None
    return {"error_tags": sorted(set(matched)), "cause": "remote Python import or runtime initialization failed"}


def startup_failure_details(client, execution_id: str | None) -> dict[str, Any]:
    if not execution_id:
        return {}
    try:
        tail = client.observe(execution_id, "tail")
    except Exception:
        return {}
    text = "\n".join(str(tail.get(key) or "") for key in ("stdout", "stderr"))
    if not text.strip():
        text = str(tail.get("tail") or "")
    errors = re.findall(r"\b[A-Z]\w*(?:Error|Exception):[^\r\n]+", text)
    imports = [line for line in errors if line.startswith(("ModuleNotFoundError:", "ImportError:"))]
    details: dict[str, Any] = {}
    if errors:
        details["log_error"] = (imports or errors)[-1][-1000:]
    diagnosis = diagnose_env_failure(text)
    if diagnosis:
        details["env_diagnosis"] = diagnosis
    return details


def merge_with_previous(previous: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    merged = dict(previous)
    for key in ("model", "served_model_name", "tp", "dp", "devices", "host", "recipe", "python_abi", "cann"):
        if overrides.get(key) not in (None, ""):
            merged[key] = overrides[key]
    if overrides.get("allow_external_busy") is not None:
        merged["allow_external_busy"] = overrides["allow_external_busy"]
    env = dict(merged.get("env") or {})
    for key in overrides.get("unset_env") or []:
        env.pop(key, None)
    env.update(overrides.get("extra_env") or {})
    merged["env"] = env
    args = list(merged.get("extra_args") or [])
    unset = overrides.get("unset_args") or []
    if unset:
        cleaned: list[str] = []
        i = 0
        while i < len(args):
            arg = args[i]
            if any(arg.startswith(u) for u in unset):
                if "=" not in arg and i + 1 < len(args) and not args[i + 1].startswith("-"):
                    i += 1
                i += 1
                continue
            cleaned.append(arg)
            i += 1
        args = cleaned
    args.extend(overrides.get("extra_args") or [])
    merged["extra_args"] = args
    return merged


def classify_run_state(state: str) -> str:
    if state in DONE:
        return "terminal"
    if state in RUNNING:
        return "running"
    if state in PENDING:
        return "pending"
    return "pending"


def wait_for_launch(client, reply: dict[str, Any], deadline: float) -> dict[str, Any]:
    """Follow the submitted execution through preparation using its owner."""
    execution_id = reply.get("execution_id")
    last_progress = None
    while execution_id and classify_run_state(str(reply.get("state") or "")) == "pending":
        state = reply.get("state")
        step = (reply.get("progress") or {}).get("step")
        current = (state, step)
        if current != last_progress:
            emit_progress("prepare", str(step or state), state=state, execution_id=execution_id)
            last_progress = current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**reply, "wait_timed_out": True}
        reply = client.wait(execution_id, until="running",
                            timeout_seconds=min(15, remaining), poll_interval=2)
    return reply


def parse_sources(value: str) -> dict[str, str]:
    try:
        sources = json.loads(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sources must be a JSON object of repository names and paths") from exc
    if not isinstance(sources, dict) or not all(isinstance(path, str) and path for path in sources.values()):
        raise argparse.ArgumentTypeError("sources must be a JSON object of repository names and paths")
    return sources


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--context-file")
    parser.add_argument("--sources", type=parse_sources,
                        help="optional repository-to-path JSON for this run; omitted uses native task defaults")
    parser.add_argument("--service", default=SERVICE_NAME, help="task-scoped business name")
    parser.add_argument("--preset")
    parser.add_argument("--model")
    parser.add_argument("--served-model-name", "--served-name", dest="served_model_name")
    parser.add_argument("--tp", "--tensor-parallel-size", dest="tp", type=int)
    parser.add_argument("--dp", "--data-parallel-size", dest="dp", type=int)
    parser.add_argument("--devices")
    parser.add_argument("--host", help="coordinator placement host (before --)")
    parser.add_argument("--allow-external-busy", action=argparse.BooleanOptionalAction, default=None,
                        help="allow an explicitly selected single card to have external workers when authorized")
    parser.add_argument("--extra-env", action="append", default=[])
    parser.add_argument("--unset-env", action="append", default=[])
    parser.add_argument("--unset-args", action="append", default=[])
    parser.add_argument("--relaunch", action="store_true", help="replace a live named service (TaskClient restart=True)")
    parser.add_argument("--port", type=int)
    parser.add_argument("--health-timeout", type=int, default=DEFAULT_HEALTH_TIMEOUT)
    parser.add_argument("--no-wait", action="store_true",
                        help="return the execution receipt without waiting for launch or HTTP readiness")
    wrapper = parser.add_mutually_exclusive_group()
    wrapper.add_argument("--wrap-script", default="", help="existing remote wrapper script")
    wrapper.add_argument("--wrap-script-local", default="", help="local UTF-8 wrapper embedded in this managed execution")
    parser.add_argument("--npu-count", type=int)
    parser.add_argument("--recipe", help="named coordinator environment recipe")
    parser.add_argument("--python-abi", dest="python_abi")
    parser.add_argument("--cann")
    parser.add_argument("--soc")
    parser.add_argument("--machine-type", dest="machine_type")
    return parser


def _parse_extra_env(items: list[str]) -> dict[str, str]:
    extra: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"bad --extra-env {item!r}, expected KEY=VALUE")
        key, _, value = item.partition("=")
        extra[require_env_name(key.strip())] = value
    return extra


def write_business_report(task_id: str, payload: dict[str, Any]) -> None:
    report = {key: payload.get(key) for key in (
        "model", "served_model_name", "tp", "dp", "devices", "host", "allow_external_busy", "env", "extra_args",
        "wrap_script", "wrap_script_content", "runtime_dir", "execution_id", "service", "recipe", "python_abi", "cann",
    )}
    save_serving_state(task_id, report)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    own_argv, vllm_extra = argv, []
    if "--" in argv:
        idx = argv.index("--")
        own_argv, vllm_extra = argv[:idx], argv[idx + 1 :]
    args = build_parser().parse_args(own_argv)
    for flag, value in (("--npu-count", args.npu_count), ("--tp", args.tp), ("--dp", args.dp)):
        if value is not None and value <= 0:
            print_json({"status": "needs_input", "error": f"{flag} must be positive"})
            return 1
    preset = load_preset(args.preset) if args.preset else None
    if preset:
        if args.tp is None and preset.get("tp") is not None:
            args.tp = int(preset["tp"])
        if args.dp is None and preset.get("dp") is not None:
            args.dp = int(preset["dp"])
        if args.port is None and preset.get("port") is not None:
            args.port = int(preset["port"])
        if not args.devices and preset.get("devices"):
            args.devices = str(preset["devices"])
        if not args.served_model_name and preset.get("served_model_name"):
            args.served_model_name = str(preset["served_model_name"])
        if args.health_timeout == DEFAULT_HEALTH_TIMEOUT and preset.get("health_timeout"):
            args.health_timeout = int(preset["health_timeout"])
        if not vllm_extra and preset.get("serve_args"):
            vllm_extra = [str(item) for item in preset["serve_args"]]
    try:
        extra_env = reject_reserved_env(_parse_extra_env(args.extra_env))
        if preset and preset.get("env"):
            extra_env = reject_reserved_env({
                **{require_env_name(str(k)): str(v) for k, v in (preset["env"] or {}).items()},
                **extra_env,
            })
        client = task_client(args.context_file)
        task_id = task_id_of(client)
        previous = load_serving_state(task_id, service=args.service)
        if args.relaunch:
            if previous is None:
                print_json({"status": "needs_input", "error": "no previous business config to relaunch"})
                return 1
            merged = merge_with_previous(
                previous,
                model=args.model,
                served_model_name=args.served_model_name,
                tp=args.tp,
                dp=args.dp,
                devices=args.devices,
                host=args.host,
                allow_external_busy=args.allow_external_busy,
                recipe=args.recipe,
                python_abi=args.python_abi,
                cann=args.cann,
                extra_env=extra_env,
                unset_env=args.unset_env,
                extra_args=vllm_extra,
                unset_args=args.unset_args,
            )
            model = merged["model"]
            served_model_name = merged["served_model_name"]
            tp, dp, devices = merged.get("tp"), merged.get("dp"), merged.get("devices")
            launch_env = reject_reserved_env(merged.get("env") or {})
            launch_extra_args = list(merged.get("extra_args") or [])
            wrap_script = args.wrap_script or str(merged.get("wrap_script") or "")
            wrap_script_content = str(merged.get("wrap_script_content") or "") if not args.wrap_script else ""
            args.recipe = merged.get("recipe") or args.recipe
            args.python_abi = merged.get("python_abi") or args.python_abi
            args.cann = merged.get("cann") or args.cann
            args.host = merged.get("host") or args.host
            args.allow_external_busy = merged.get("allow_external_busy", False)
        else:
            if not args.model:
                print_json({"status": "needs_input", "error": "--model is required for a fresh start"})
                return 1
            model = args.model
            served_model_name = args.served_model_name or Path(model).name
            tp, dp, devices = args.tp, args.dp, args.devices
            launch_env = extra_env
            launch_extra_args = vllm_extra
            wrap_script = args.wrap_script or ""
            wrap_script_content = ""
        if args.wrap_script_local:
            wrap_script_content = Path(args.wrap_script_local).read_text(encoding="utf-8")
            if not wrap_script_content.strip() or len(wrap_script_content.encode("utf-8")) > 1024 * 1024:
                raise ValueError("local wrapper must be nonempty UTF-8 text at most 1 MiB")
            wrap_script = ""
        runtime_dir = "/tmp/vaws-serve." + uuid.uuid4().hex if wrap_script or wrap_script_content else ""
        problems = local_preset_problems(preset, launch_extra_args)
        if problems:
            print_json({"status": "needs_input", "phase": "preflight", "problems": problems})
            return 1
        device_list = parse_devices_csv(devices) if devices else []
        npu_count = args.npu_count if args.npu_count is not None else (int(tp) * int(dp or 1) if tp is not None else 1)
        if args.allow_external_busy and (len(device_list) != 1 or int(tp or 1) * int(dp or 1) != 1):
            print_json({"status": "needs_input", "error": "--allow-external-busy requires one explicit --devices card and TP1/DP1"})
            return 1
        if args.allow_external_busy:
            parallel_flags = {"--tensor-parallel-size", "-tp", "--data-parallel-size", "-dp",
                              "--pipeline-parallel-size", "-pp", "--data-parallel-size-local", "-dpl",
                              "--decode-context-parallel-size", "-dcp", "--prefill-context-parallel-size", "-pcp",
                              "--nnodes", "--config"}
            options = [arg.split("=", 1)[0].replace("_", "-") for arg in launch_extra_args if arg.startswith("-")]
            # vLLM accepts abbreviated options and YAML configuration, which
            # could otherwise replace the single-card topology after validation.
            if any(flag.startswith(option) for option in options for flag in parallel_flags):
                print_json({"status": "needs_input", "error": "shared single-card serving takes TP/DP through wrapper --tp/--dp; extra parallel-size overrides and --config are unsupported"})
                return 1
        command = build_serve_command(
            model=model,
            served_model_name=served_model_name,
            tp=tp,
            dp=dp,
            extra_args=launch_extra_args,
            wrap_script=wrap_script,
            wrap_script_content=wrap_script_content,
            runtime_dir=runtime_dir,
            expected_vllm=str((preset or {}).get("vllm_version") or ""),
        )
        business_report = {
            "model": model,
            "served_model_name": served_model_name,
            "tp": tp,
            "dp": dp,
            "devices": devices,
            "host": args.host,
            "allow_external_busy": bool(args.allow_external_busy),
            "env": launch_env,
            "extra_args": launch_extra_args,
            "wrap_script": wrap_script or None,
            "wrap_script_content": wrap_script_content or None,
            "runtime_dir": runtime_dir or None,
            "service": args.service,
            "recipe": args.recipe,
            "python_abi": args.python_abi,
            "cann": args.cann,
        }
        if device_list and args.npu_count is not None:
            print_json({
                "status": "needs_input",
                "error": "pass --devices or --npu-count, not both",
            })
            return 1
        resources = service_resources(
            npu_count=None if device_list else npu_count,
            devices=device_list or None,
            service_port=int(args.port) if args.port is not None else 0,
            allow_external_busy=bool(args.allow_external_busy),
        )
        environment = named_environment(
            recipe=args.recipe,
            python_abi=args.python_abi,
            cann=args.cann,
            soc=args.soc,
            machine_type=args.machine_type,
            preset=preset,
        )
        emit_progress("launch", "submitting managed vLLM execution")
        deadline = time.monotonic() + args.health_timeout
        reply = run_command(
            client,
            command,
            sources=args.sources,
            env=launch_env,
            environment=environment,
            resources=resources,
            timeout_seconds=None,
            service=args.service,
            restart=bool(args.relaunch),
            **({"topology": {"host": args.host}} if args.host else {}),
        )
        business_report["execution_id"] = reply.get("execution_id")
        write_business_report(task_id, business_report)
        if not args.no_wait:
            reply = wait_for_launch(client, reply, deadline)
        state = str(reply.get("state") or "")
        kind = classify_run_state(state)
        execution_id = reply.get("execution_id")
        output: dict[str, Any] = {
            "task_id": task_id,
            "service": args.service,
            "execution_id": execution_id,
            "state": state,
            "model": model,
            "served_model_name": served_model_name,
            "tp": tp,
            "dp": dp,
            "devices": devices,
            "runtime_dir": runtime_dir or None,
            **execution_summary(reply),
        }
        if kind == "pending" or (args.no_wait and kind == "running"):
            output["status"] = state
            output["running"] = kind == "running"
            output["ready"] = False
            if reply.get("wait_timed_out"):
                output["wait_timed_out"] = True
                output["error"] = "startup wait timed out; use status with the same service or execution reference"
            print_json(output)
            return 0
        if kind == "terminal":
            output["status"] = "failed"
            output.update(startup_failure_details(client, execution_id))
            output["error"] = reply.get("reason") or reply.get("error") or output.get("log_error") or f"execution ended in {state}"
            print_json(output)
            return 1
        port = service_port_of(reply)
        if port is None:
            output["status"] = "incomplete"
            output["running"] = True
            output["error"] = "execution is running but has no service port yet"
            print_json(output)
            return 1
        endpoint = endpoint_from_reply(reply)

        def still_running() -> bool:
            observation = client.observe(execution_id, "status") if execution_id else reply
            return classify_run_state(str(observation.get("state") or "")) == "running"

        def log_text() -> str:
            if not execution_id:
                return ""
            try:
                tail = client.observe(execution_id, "tail")
            except Exception:
                return ""
            return str(tail.get("tail") or tail.get("stdout") or "")[-4000:]

        emit_progress("probe", f"waiting for ready (timeout={args.health_timeout}s)")
        readiness = wait_for_ready(
            endpoint, port, max(0, deadline - time.monotonic()), served_model_name,
            still_running=still_running, log_text=log_text,
        )
        output["port"] = port
        output["base_url"] = f"http://{endpoint.host}:{port}"
        output["readiness"] = readiness
        if readiness.get("ready"):
            output["status"] = "ready"
            output["running"] = True
            output["ready"] = True
            print_json(output)
            return 0
        output["status"] = "incomplete"
        output["running"] = bool(readiness.get("running"))
        output["ready"] = False
        output["error"] = readiness.get("error") or "service did not become ready"
        diagnosis = diagnose_env_failure(log_text())
        if diagnosis:
            output["env_diagnosis"] = diagnosis
        print_json(output)
        return 1
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2
