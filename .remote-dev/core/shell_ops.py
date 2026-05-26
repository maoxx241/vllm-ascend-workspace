from __future__ import annotations

import shlex
import time
import re
from typing import Any

from .endpoint import Endpoint
from .errors import PathPolicyError
from .job_ops import start_remote_job
from .path_policy import assert_under_root
from .preview import compact_text, stdout_stderr_preview
from .result import make_result, new_invocation_id, utc_now_iso
from .ssh_transport import run_script
from .state_store import atomic_write_json, atomic_write_text, new_log_dir

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


def _env_exports(env: dict[str, str]) -> list[str]:
    lines = []
    for key, value in sorted(env.items()):
        if not ENV_NAME_RE.fullmatch(key):
            raise ValueError(f"invalid environment variable name: {key!r}")
        lines.append(f"export {key}={shlex.quote(str(value))}")
    return lines


def _cwd_outside_root_next(cwd: str) -> dict[str, Any]:
    if not cwd.startswith("/"):
        return {
            "suggested_action": "rerun_with_absolute_cwd",
            "message": "Remote cwd must be absolute and inside the endpoint root.",
        }
    return {
        "suggested_action": "rerun_with_endpoint_root",
        "message": (
            "The requested cwd is outside the endpoint root. If this path is "
            "intentional and trusted, rerun with root set to cwd or one of "
            "its trusted ancestor directories."
        ),
        "endpoint_patch": {"root": cwd, "cwd": cwd},
    }


def remote_bash(
    endpoint: Endpoint,
    *,
    command: str,
    cwd: str | None = None,
    description: str | None = None,
    timeout_ms: int | None = 120000,
    run_in_background: bool = False,
    runtime_env: bool | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = env or {}
    runtime_enabled = endpoint.runtime_env if runtime_env is None else runtime_env
    try:
        cwd = assert_under_root(cwd or endpoint.effective_cwd, endpoint.root)
    except PathPolicyError as exc:
        result = make_result(
            tool="remote.bash",
            target=endpoint.to_result_target(),
            outcome="blocked",
            status="cwd_outside_root",
            summary="RemoteBash blocked because cwd is outside root.",
            preview={"stderr": str(exc)},
            next=_cwd_outside_root_next(cwd or endpoint.effective_cwd),
            extra={"error": str(exc), "command_preview": command[:500]},
        )
        text = (
            result["summary"]
            + "\n"
            + str(exc)
            + "\n"
            + f"Next: rerun with --root {shlex.quote(cwd or endpoint.effective_cwd)} "
            + f"--cwd {shlex.quote(cwd or endpoint.effective_cwd)} if that path is trusted.\n"
        )
        return {"text": text, "result": result}
    if run_in_background:
        return start_remote_job(
            endpoint,
            command=command,
            cwd=cwd,
            env=env,
            timeout_ms=timeout_ms,
            runtime_env=runtime_enabled,
            description=description,
        )

    invocation_id = new_invocation_id()
    started = utc_now_iso()
    start = time.monotonic()
    log_dir = new_log_dir(endpoint, "bash", invocation_id)
    runtime_lines = [
        "if [ -f /etc/profile.d/vaws-ascend-env.sh ]; then set +u; . /etc/profile.d/vaws-ascend-env.sh; set -u; fi"
    ] if runtime_enabled else []
    validation = "\n".join(
        [
            "python3 - <<'REMOTE_DEV_VALIDATE'",
            "import pathlib, sys",
            f"root = pathlib.Path({endpoint.root!r}).resolve()",
            f"cwd = pathlib.Path({cwd!r})",
            "if not cwd.exists():",
            "    print('REMOTE_DEV_CWD_NOT_FOUND', file=sys.stderr)",
            "    raise SystemExit(70)",
            "resolved = cwd.resolve()",
            "if resolved != root and root not in resolved.parents:",
            "    print('REMOTE_DEV_CWD_OUTSIDE_ROOT', file=sys.stderr)",
            "    raise SystemExit(71)",
            "REMOTE_DEV_VALIDATE",
        ]
    )
    script = "\n".join(
        [
            "set -u",
            validation,
            *runtime_lines,
            f"cd {shlex.quote(cwd)} || exit 70",
            *_env_exports(env),
            f"REMOTE_DEV_COMMAND={shlex.quote(command)}",
            'bash -c "$REMOTE_DEV_COMMAND"',
        ]
    )
    completed = run_script(endpoint, script, timeout_ms=timeout_ms)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    result_path = log_dir / "result.json"
    atomic_write_text(stdout_path, completed.stdout)
    atomic_write_text(stderr_path, completed.stderr)
    if completed.timed_out:
        outcome = "timeout"
        status = "timeout"
        summary = "RemoteBash timed out."
    elif completed.returncode == 0:
        outcome = "success"
        status = "ok"
        summary = "RemoteBash completed successfully."
    elif completed.returncode == 70:
        outcome = "failed"
        status = "cwd_not_found"
        summary = "RemoteBash failed because cwd does not exist."
    elif completed.returncode == 71:
        outcome = "blocked"
        status = "cwd_outside_root"
        summary = "RemoteBash blocked because cwd is outside root."
    else:
        outcome = "failed"
        status = "nonzero_exit"
        summary = f"RemoteBash exited with code {completed.returncode}."
    result = make_result(
        tool="remote.bash",
        target={**endpoint.to_result_target(), "cwd": cwd},
        outcome=outcome,  # type: ignore[arg-type]
        status=status,
        summary=summary,
        invocation_id=invocation_id,
        started_at=started,
        duration_ms=_duration_ms(start),
        preview=stdout_stderr_preview(completed.stdout, completed.stderr),
        refs={"stdout": str(stdout_path), "stderr": str(stderr_path), "metadata": str(result_path)},
        extra={
            "exit_code": completed.returncode,
            "timed_out": completed.timed_out,
            "command_preview": command[:500],
            "environment": {"runtime_env": runtime_enabled, "env_keys": sorted(env), "timeout_ms": timeout_ms},
        },
    )
    atomic_write_json(result_path, result)
    return {"text": _format_bash_text(endpoint, cwd, result), "result": result}


def _preview_text(item: Any) -> str:
    if isinstance(item, dict):
        if "text" in item:
            return str(item["text"])
        if "head" in item or "tail" in item:
            return f"{item.get('head', '')}\n...\n{item.get('tail', '')}".strip()
    return str(item or "")


def _format_bash_text(endpoint: Endpoint, cwd: str, result: dict[str, Any]) -> str:
    preview = result.get("preview", {})
    lines = [
        f"RemoteBash completed on {endpoint.user}@{endpoint.host}:{endpoint.port}",
        f"cwd: {cwd}",
        f"exit code: {result.get('exit_code')}",
        f"duration: {round((result.get('duration_ms') or 0) / 1000, 3)}s",
        "",
        "stdout:",
        _preview_text(preview.get("stdout")),
        "",
        "stderr:",
        _preview_text(preview.get("stderr")) or "<empty>",
        "",
        "Full logs:",
        str(result.get("refs", {}).get("stdout")),
        str(result.get("refs", {}).get("stderr")),
    ]
    return compact_text("\n".join(lines).rstrip() + "\n")
