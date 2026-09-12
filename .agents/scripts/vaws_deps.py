#!/usr/bin/env python3
"""Inspect, sync, and report the workspace package dependencies.

Subcommands:

    status [name...]    JSON inspect payload; exit 1 unless every name is ready
    doctor              Result Envelope v1 capability report
    sync                prepare/reuse an immutable locked environment

Progress goes to stderr. Each command prints one JSON object on stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import REMEDY, configure_windows_stdio, ensure_workspace_interpreter
from vaws_environment import EnvironmentError, PIN_ENV, prepare_environment
from vaws_knowledge_service import prepare_knowledge


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _names(requested: list[str] | None) -> list[str]:
    from vaws_dependency import DependencyError, KNOWN_NAMES
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
    from vaws_dependency import DependencyError, inspect, status_exit_code
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
    from vaws_capability import build_doctor_envelope, dumps_doctor, dumps_doctor_view
    from vaws_dependency import DependencyError
    from vaws_result_envelope import default_record_dir
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
    progress("selecting or preparing the immutable locked dependency environment")
    try:
        receipt = prepare_environment(ROOT, install_options=extra)
    except (EnvironmentError, OSError) as exc:
        _print({"ok": False, "error": str(exc), "remedy": REMEDY})
        return 1
    if os.name == "nt":
        from vaws_environment_link import link_environment
        link_environment(ROOT, key=receipt["key"], environment_root=Path(receipt["root"]))
    payload = {"ok": True, "returncode": 0, "environment": receipt["root"], "receipt": receipt, "remedy": None}
    progress("dependencies ready; preparing local knowledge model and index")
    previous_pin = os.environ.get(PIN_ENV)
    os.environ[PIN_ENV] = receipt["receipt"]
    try:
        payload["knowledge"] = prepare_knowledge(ROOT)
    finally:
        if previous_pin is None:
            os.environ.pop(PIN_ENV, None)
        else:
            os.environ[PIN_ENV] = previous_pin
    if not payload["knowledge"].get("ready"):
        progress("dependencies are ready; knowledge preparation is pending and does not block ordinary tools")
    _print(payload)
    return 0


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

    sync = sub.add_parser("sync", help="prepare a locked environment; progress on stderr, JSON on stdout")
    sync.add_argument("passthrough", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    sync.set_defaults(func=cmd_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_windows_stdio()
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if args.command == "sync":
        args.passthrough = extra + list(args.passthrough or [])
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if args.command != "sync":
        ensure_workspace_interpreter(repo_root=ROOT)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
