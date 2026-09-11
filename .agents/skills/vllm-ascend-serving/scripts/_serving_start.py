#!/usr/bin/env python3
"""Start a vllm-ascend service through one coordinator TaskClient.run call."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

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
from vaws_local_state import effective_workspace_alias, load_workspace_identity  # noqa: E402
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
    expected_vllm: str = "",
    preflight_only: bool = False,
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
    if preflight_only:
        parse_only = "\n".join([
            "import sys",
            "from vllm.entrypoints.cli.serve import cmd_init",
            "from vllm.utils.argparse_utils import FlexibleArgumentParser",
            "parser = FlexibleArgumentParser(prog='vllm')",
            "subparsers = parser.add_subparsers(dest='subparser')",
            "for command in cmd_init(): command.subparser_init(subparsers)",
            "parser.parse_args(sys.argv[1:])",
        ])
        lines.append('"$VAWS_PYTHON" -c ' + shlex.quote(parse_only) + " " + " ".join(argv[3:]))
        return "\n".join(lines)
    if wrap_script:
        lines.append("runtime_dir=$(mktemp -d /tmp/vaws-serve.XXXXXX)")
        lines.append("cat > \"$runtime_dir/_serve.sh\" << 'VAWS_SERVE_EOF'")
        lines.append("#!/bin/bash")
        lines.append(f'if [ -z "${{VAWS_PYTHON:-}}" ]; then echo "VAWS_PYTHON is unset" >&2; exit 1; fi')
        lines.append(f"exec {cmd_str}")
        lines.append("VAWS_SERVE_EOF")
        lines.append("chmod +x \"$runtime_dir/_serve.sh\"")
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
    return {"error_tags": sorted(set(matched)), "cause": "remote Python package version mismatch"}


def merge_with_previous(previous: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    merged = dict(previous)
    for key in ("model", "served_model_name", "tp", "dp", "devices", "recipe", "python_abi", "cann"):
        if overrides.get(key) not in (None, ""):
            merged[key] = overrides[key]
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--context-file")
    parser.add_argument("--service", default=SERVICE_NAME, help="task-scoped business name")
    parser.add_argument("--preset")
    parser.add_argument("--model")
    parser.add_argument("--served-model-name", "--served-name", dest="served_model_name")
    parser.add_argument("--tp", "--tensor-parallel-size", dest="tp", type=int)
    parser.add_argument("--dp", "--data-parallel-size", dest="dp", type=int)
    parser.add_argument("--devices")
    parser.add_argument("--extra-env", action="append", default=[])
    parser.add_argument("--unset-env", action="append", default=[])
    parser.add_argument("--unset-args", action="append", default=[])
    parser.add_argument("--relaunch", action="store_true", help="replace a live named service (TaskClient restart=True)")
    parser.add_argument("--port", type=int)
    parser.add_argument("--health-timeout", type=int, default=DEFAULT_HEALTH_TIMEOUT)
    parser.add_argument("--wrap-script", default="")
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
        "model", "served_model_name", "tp", "dp", "devices", "env", "extra_args",
        "wrap_script", "service", "recipe", "python_abi", "cann",
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
        previous = load_serving_state(task_id)
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
            args.recipe = merged.get("recipe") or args.recipe
            args.python_abi = merged.get("python_abi") or args.python_abi
            args.cann = merged.get("cann") or args.cann
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
        problems = local_preset_problems(preset, launch_extra_args)
        if problems:
            print_json({"status": "needs_input", "phase": "preflight", "problems": problems})
            return 1
        identity = load_workspace_identity()
        alias = effective_workspace_alias()
        if identity is not None:
            launch_env["VAWS_AGENT_ID"] = identity["agent_id"]
        if alias:
            launch_env["VAWS_AGENT_ALIAS"] = alias
        device_list = parse_devices_csv(devices) if devices else []
        npu_count = args.npu_count or (int(tp) * int(dp or 1) if tp is not None else 1)
        command = build_serve_command(
            model=model,
            served_model_name=served_model_name,
            tp=tp,
            dp=dp,
            extra_args=launch_extra_args,
            wrap_script=wrap_script,
            expected_vllm=str((preset or {}).get("vllm_version") or ""),
        )
        write_business_report(task_id, {
            "model": model,
            "served_model_name": served_model_name,
            "tp": tp,
            "dp": dp,
            "devices": devices,
            "env": launch_env,
            "extra_args": launch_extra_args,
            "wrap_script": wrap_script or None,
            "service": args.service,
            "recipe": args.recipe,
            "python_abi": args.python_abi,
            "cann": args.cann,
        })
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
        reply = run_command(
            client,
            command,
            preflight=build_serve_command(
                model=model, served_model_name=served_model_name, tp=tp, dp=dp,
                extra_args=launch_extra_args, preflight_only=True,
                expected_vllm=str((preset or {}).get("vllm_version") or "")),
            env=launch_env,
            environment=environment,
            resources=resources,
            timeout_seconds=None,
            service=args.service,
            restart=bool(args.relaunch),
        )
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
            **execution_summary(reply),
        }
        if kind == "pending":
            output["status"] = state
            output["running"] = False
            output["ready"] = False
            print_json(output)
            return 0
        if kind == "terminal":
            output["status"] = "failed"
            output["error"] = reply.get("reason") or reply.get("error") or f"execution ended in {state}"
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
            endpoint, port, args.health_timeout, served_model_name,
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
