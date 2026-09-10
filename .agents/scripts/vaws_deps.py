#!/usr/bin/env python3
"""Inspect, sync, and report the workspace package dependencies.

Subcommands:

    status [name...]    JSON inspect payload; exit 1 unless every name is ready
    doctor              Result Envelope v1 capability report
    sync                wrap ``uv sync`` (progress on stderr, JSON on stdout)

Progress goes to stderr. Each command prints one JSON object on stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_capability import build_doctor_envelope, dumps_doctor, dumps_doctor_view  # noqa: E402
from vaws_result_envelope import default_record_dir  # noqa: E402
from vaws_dependency import (  # noqa: E402
    DependencyError,
    KNOWN_NAMES,
    REMEDY,
    all_packages,
    inspect,
    status_exit_code,
)


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _names(requested: list[str] | None) -> list[str]:
    known = list(KNOWN_NAMES)
    if not requested:
        return known
    unknown = [name for name in requested if name not in known]
    if unknown:
        raise DependencyError(
            f"unknown dependency {unknown[0]!r}; known: {known}",
            field="$.name",
        )
    return requested


def cmd_status(args: argparse.Namespace) -> int:
    try:
        names = _names(list(args.names or []))
    except DependencyError as exc:
        progress(str(exc))
        _print({"error": str(exc), "field": exc.field})
        return 2
    progress(f"inspecting {', '.join(names)}")
    deps = {name: inspect(name) for name in names}
    payload = deps if len(names) != 1 else deps[names[0]]
    _print(payload)
    return status_exit_code({name: deps[name]["state"] for name in names})


def cmd_doctor(args: argparse.Namespace) -> int:
    argv = ["python3", ".agents/scripts/vaws_deps.py", "doctor", *list(args.passthrough or [])]
    progress("collecting workspace capability report")
    try:
        envelope = build_doctor_envelope(argv=argv)
    except DependencyError as exc:
        progress(f"invalid spec: {exc}")
        envelope = build_doctor_envelope(argv=argv, pin_error=exc)
    full = bool(getattr(args, "full", False)) or os.environ.get("VAWS_FULL_ENVELOPE") == "1"
    if full:
        sys.stdout.write(dumps_doctor(envelope) + "\n")
    else:
        sys.stdout.write(
            dumps_doctor_view(
                envelope,
                full=False,
                record_dir=default_record_dir(ROOT),
            )
            + "\n"
        )
    sys.stdout.flush()
    code = envelope.get("exit_code")
    return code if isinstance(code, int) else 1


def cmd_sync(args: argparse.Namespace) -> int:
    extra = list(args.passthrough or [])
    command = ["uv", "sync", *extra]
    progress(f"running {' '.join(command)}")
    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            check=False,
        )
    except FileNotFoundError:
        payload = {
            "ok": False,
            "command": command,
            "error": "uv is not on PATH",
            "remedy": REMEDY,
        }
        _print(payload)
        return 1
    payload = {
        "ok": proc.returncode == 0,
        "command": command,
        "returncode": proc.returncode,
        "packages": all_packages() if proc.returncode == 0 else None,
        "remedy": None if proc.returncode == 0 else REMEDY,
    }
    _print(payload)
    return 0 if proc.returncode == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="inspect one or more installed packages")
    status.add_argument("names", nargs="*", help="package names (default: all)")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="emit a compact capability view; --full for the envelope")
    doctor.add_argument("--full", action="store_true", help="print the complete Result Envelope")
    doctor.add_argument("passthrough", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    doctor.set_defaults(func=cmd_doctor)

    sync = sub.add_parser("sync", help="wrap uv sync; progress on stderr, JSON on stdout")
    sync.add_argument("passthrough", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    sync.set_defaults(func=cmd_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
