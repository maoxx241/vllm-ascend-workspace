#!/usr/bin/env python3
"""Create and validate workspace Run Manifest v1 files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_run_manifest import (  # noqa: E402
    RUN_TYPES,
    RunManifestError,
    load_manifest,
    new_manifest,
    write_manifest,
)


def _json_object(raw: str, label: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError(f"{label} must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    init = subparsers.add_parser("init", help="create a planned manifest")
    init.add_argument("--run-type", required=True, choices=sorted(RUN_TYPES))
    init.add_argument("--run-id")
    init.add_argument("--parent-run-id")
    init.add_argument("--output", required=True, type=Path)
    init.add_argument("--workspace-snapshot", default="{}")
    init.add_argument("--environment", default="{}")
    init.add_argument("--model", default="{}")
    init.add_argument("--topology", default="{}")
    init.add_argument("--command", action="append", default=[])
    init.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")

    validate = subparsers.add_parser("validate", help="validate an existing manifest")
    validate.add_argument("manifest", type=Path)
    return parser


def _parse_env(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise RunManifestError(f"environment item must use NAME=VALUE: {item!r}")
        key, value = item.split("=", 1)
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "init":
            manifest = new_manifest(
                run_type=args.run_type,
                run_id=args.run_id,
                parent_run_id=args.parent_run_id,
                workspace_snapshot=_json_object(
                    args.workspace_snapshot, "workspace-snapshot"
                ),
                environment=_json_object(args.environment, "environment"),
                model=_json_object(args.model, "model"),
                topology=_json_object(args.topology, "topology"),
                command=args.command,
                environment_variables=_parse_env(args.env),
            )
            write_manifest(args.output, manifest)
            payload = {
                "status": "created",
                "manifest": str(args.output.resolve()),
                "run_id": manifest["run_id"],
                "run_type": manifest["run_type"],
            }
        else:
            manifest = load_manifest(args.manifest)
            payload = {
                "status": "passed",
                "manifest": str(args.manifest.resolve()),
                "run_id": manifest["run_id"],
                "run_type": manifest["run_type"],
                "run_status": manifest["status"],
            }
    except (RunManifestError, argparse.ArgumentTypeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stdout)
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
