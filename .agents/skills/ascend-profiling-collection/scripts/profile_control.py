#!/usr/bin/env python3
"""POST /start_profile or /stop_profile against a running vllm-ascend service.

Profiler control is part of the *profiling collection* workflow, not part of
service lifecycle, so this client lives inside the profiling-collection skill
rather than the serving skill. The serving skill is intentionally agnostic:
it just passes ``--profiler-config`` through to ``vllm serve`` and never
touches the profiler window.

The POST is executed inside the container via SSH against
``http://127.0.0.1:<port>``, so no SSH tunnel is required. The port comes
from the named coordinator service or ``--execution-id``. A service must
already be running.

Multi-rank torch profiler setup/finalization can take much longer than an
ordinary inference request, so the timeout is long by default.

Usage:
    python3 profile_control.py --action start_profile --service vllm
    python3 profile_control.py --execution-id <id> --action start_profile
    python3 profile_control.py --execution-id <id> --action stop_profile [--timeout 900]

Progress on stderr, final JSON on stdout.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

if __name__ == "__main__":
    from vaws_managed_entry import ensure_managed_entry
    ensure_managed_entry(repo_root=ROOT, entry_file=__file__)

ensure_workspace_interpreter(repo_root=ROOT)

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import emit_progress, endpoint_from_reply, print_json, service_port_of, ssh_exec  # noqa: E402
from vaws_task_target import task_client  # noqa: E402

DEFAULT_TIMEOUT_SECONDS = 600
ALLOWED_ACTIONS = ("start_profile", "stop_profile")


def post_remote_action(ep, port: int, action: str, timeout: int) -> dict[str, Any]:
    """POST http://127.0.0.1:<port>/<action> from inside the container.

    Returns a dict with: ok, status, body. Raises RuntimeError on transport
    failure (SSH problem, container Python missing, etc.).
    """
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"unsupported action {action!r}; allowed: {ALLOWED_ACTIONS}")

    py = (
        "import json, urllib.error, urllib.request\n"
        f"url = 'http://127.0.0.1:{port}/{action}'\n"
        "req = urllib.request.Request(url, data=None,\n"
        "    headers={'Content-Type': 'application/json'}, method='POST')\n"
        "try:\n"
        f"    with urllib.request.urlopen(req, timeout={int(timeout)}) as resp:\n"
        "        body = resp.read().decode('utf-8', errors='replace')\n"
        "        print(json.dumps({'ok': True, 'status': resp.status, 'body': body}))\n"
        "except urllib.error.HTTPError as exc:\n"
        "    body = exc.read().decode('utf-8', errors='replace')\n"
        "    print(json.dumps({'ok': False, 'status': exc.code, 'body': body}))\n"
        "    raise SystemExit(2)\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'ok': False, 'error': str(exc)}))\n"
        "    raise SystemExit(3)\n"
    )
    script = f"python3 -c {shlex.quote(py)}"
    result = ssh_exec(ep, script, check=False)

    payload: dict[str, Any] | None = None
    stdout = (result.stdout or "").strip()
    if stdout:
        last_line = stdout.splitlines()[-1]
        try:
            payload = json.loads(last_line)
        except json.JSONDecodeError:
            payload = None

    if result.returncode != 0:
        detail: dict[str, Any] = payload or {}
        detail.setdefault("stdout", (result.stdout or "")[-2000:])
        detail.setdefault("stderr", (result.stderr or "")[-2000:])
        detail["rc"] = result.returncode
        raise RuntimeError(
            f"remote {action} on 127.0.0.1:{port} failed: "
            f"{json.dumps(detail, ensure_ascii=False)}"
        )

    return payload or {"ok": True, "status": 200, "body": ""}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--context-file")
    p.add_argument("--execution-id")
    p.add_argument("--service", default="vllm")
    p.add_argument(
        "--action",
        required=True,
        choices=ALLOWED_ACTIONS,
        help="profiler control action to issue",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=(
            "Timeout in seconds for the control-plane HTTP request. "
            "Multi-rank torch-profiler setup/finalization can take much longer "
            "than an ordinary inference request."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        client = task_client(args.context_file)
        observation = client.observe(args.execution_id, service=None if args.execution_id else args.service)
        if observation.get("state") == "not_found":
            print_json({"status": "not_found", "action": args.action, "message": "no service execution"})
            return 2
        port = service_port_of(observation)
        if not port:
            print_json({"status": "not_found", "action": args.action, "message": "execution has no service port"})
            return 2
        ep = endpoint_from_reply(observation)
        emit_progress("profile_control", f"posting {args.action} to 127.0.0.1:{port}", timeout=args.timeout)
        result = post_remote_action(ep, int(port), args.action, args.timeout)
        ok = bool(result.get("ok"))
        print_json({
            "status": "ok" if ok else "failed",
            "execution_id": observation.get("execution_id"),
            "action": args.action,
            "port": port,
            "http_status": result.get("status"),
            "body": result.get("body"),
            "error": result.get("error"),
        })
        return 0 if ok else 1

    except Exception as exc:
        print_json({
            "status": "failed",
            "execution_id": getattr(args, "execution_id", None),
            "service": getattr(args, "service", None),
            "action": getattr(args, "action", None),
            "error": str(exc),
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
