#!/usr/bin/env python3
"""Inspect, bootstrap, and report the four external VAWS dependency checkouts.

Subcommands:

    status [name...]    JSON inspect payload; exit 1 on off_pin unless allowed
    bootstrap <name|all> [--dest] [--reset]
    doctor              Result Envelope v1 capability report

Progress goes to stderr. Each command prints one JSON object on stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_capability import build_doctor_envelope, dumps_doctor  # noqa: E402
from vaws_dependency import (  # noqa: E402
    DependencyPinError,
    all_pins,
    bootstrap,
    inspect,
    load_pin,
    status_exit_code,
)


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _names(requested: list[str] | None) -> list[str]:
    known = list(all_pins())
    if not requested:
        return known
    unknown = [name for name in requested if name not in known]
    if unknown:
        raise DependencyPinError(
            f"unknown dependency {unknown[0]!r}; known: {known}",
            field="$.name",
        )
    return requested


def cmd_status(args: argparse.Namespace) -> int:
    try:
        names = _names(list(args.names or []))
    except DependencyPinError as exc:
        progress(str(exc))
        _print({"error": str(exc), "field": exc.field})
        return 2
    progress(f"inspecting {', '.join(names)}")
    deps = {name: inspect(name) for name in names}
    payload = {"deps": deps} if len(names) != 1 else deps[names[0]]
    _print(payload)
    return status_exit_code({name: deps[name]["state"] for name in names})


def cmd_bootstrap(args: argparse.Namespace) -> int:
    try:
        if args.name == "all":
            names = list(all_pins())
        else:
            load_pin(args.name)
            names = [args.name]
    except DependencyPinError as exc:
        progress(str(exc))
        _print({"error": str(exc), "field": exc.field})
        return 2
    dest = Path(args.dest).expanduser() if args.dest else None
    if dest is not None and args.name == "all":
        progress("--dest cannot be combined with bootstrap all")
        _print({"error": "--dest cannot be combined with bootstrap all"})
        return 2
    results: dict[str, object] = {}
    exit_code = 0
    for name in names:
        progress(f"bootstrapping {name}")
        payload = bootstrap(name, dest=dest, reset=args.reset)
        results[name] = payload
        if payload.get("state") not in {"ready", "off_pin"}:
            exit_code = 1
        elif payload.get("state") == "off_pin" and not args.reset:
            exit_code = 1
    _print(results if args.name == "all" else results[names[0]])
    return exit_code


def cmd_doctor(args: argparse.Namespace) -> int:
    argv = ["python3", ".agents/scripts/vaws_deps.py", "doctor", *list(args.passthrough or [])]
    progress("collecting workspace capability report")
    try:
        envelope = build_doctor_envelope(argv=argv)
    except DependencyPinError as exc:
        progress(f"invalid pin: {exc}")
        envelope = build_doctor_envelope(argv=argv, pin_error=exc)
    sys.stdout.write(dumps_doctor(envelope) + "\n")
    sys.stdout.flush()
    code = envelope.get("exit_code")
    return code if isinstance(code, int) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="inspect one or more pinned checkouts")
    status.add_argument("names", nargs="*", help="dependency names (default: all)")
    status.set_defaults(func=cmd_status)

    boot = sub.add_parser("bootstrap", help="clone or optionally reset a pinned checkout")
    boot.add_argument("name", help="dependency name, or 'all'")
    boot.add_argument("--dest", help="override the default checkout directory")
    boot.add_argument(
        "--reset",
        action="store_true",
        help="fetch and checkout the pin when the working tree is clean",
    )
    boot.set_defaults(func=cmd_bootstrap)

    doctor = sub.add_parser("doctor", help="emit a Result Envelope v1 capability report")
    doctor.add_argument("passthrough", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
