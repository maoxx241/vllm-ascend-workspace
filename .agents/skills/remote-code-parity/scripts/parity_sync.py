#!/usr/bin/env python3
"""Direct source-only inspection of a prepared container work root.

Coordinator prepares managed execution sources. Do not pass --execution-id
to mutate a live execution root. Direct host inspection uses the coordinator
parity package in source-only mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from common import WORKSPACE_ID_PATTERN, print_json, repo_root_from
from remote_code_parity import DEFAULT_CONTAINER_CACHE_ROOT, TRANSFER_MODES

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

DEFAULT_CONTAINER_USER = "root"
DEFAULT_RUNTIME_ROOT = "/vllm-workspace"


def derive_workspace_id(repo_root: Path) -> str:
    base = WORKSPACE_ID_PATTERN.sub("-", repo_root.name.lower()).strip(".-") or "workspace"
    digest = hashlib.sha1(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def build_derived_args(repo_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "execution_id", None):
        raise RuntimeError(
            "coordinator prepares managed sources; do not synchronize or rebuild "
            "into a live execution root (--execution-id)"
        )
    if not getattr(args, "host", None):
        raise RuntimeError("direct source-only inspection requires --host")
    host = str(args.host)
    port = int(getattr(args, "port", None) or 22)
    runtime_root = str(getattr(args, "runtime_root", None) or DEFAULT_RUNTIME_ROOT)
    container_name = str(getattr(args, "container_name", None) or host)
    workspace_id = args.workspace_id or derive_workspace_id(repo_root)
    return {
        "workspace_root": str(repo_root),
        "workspace_id": workspace_id,
        "server_name": container_name,
        "runtime_root": runtime_root,
        "container_identity": f"{container_name}@{runtime_root}",
        "container_cache_root": args.container_cache_root,
        "container_host": host,
        "container_port": port,
        "container_user": args.container_user,
        "preserve_path": list(args.preserve_path),
        "machine_record": None,
        "execution_id": None,
    }


def build_low_level_command(derived: dict[str, Any], args: argparse.Namespace) -> list[str]:
    script_path = Path(__file__).with_name("remote_code_parity.py")
    cmd = [
        sys.executable,
        str(script_path),
        "sync",
        "--workspace-root", derived["workspace_root"],
        "--workspace-id", derived["workspace_id"],
        "--server-name", derived["server_name"],
        "--runtime-root", derived["runtime_root"],
        "--container-identity", derived["container_identity"],
        "--container-cache-root", derived["container_cache_root"],
        "--container-host", derived["container_host"],
        "--container-port", str(derived["container_port"]),
        "--container-user", derived["container_user"],
        "--apply-mode", "source-only",
    ]
    for preserve_path in derived["preserve_path"]:
        cmd.extend(["--preserve-path", preserve_path])
    for source in getattr(args, "source", []) or []:
        cmd.extend(["--source", source])
    if args.snapshot_id:
        cmd.extend(["--snapshot-id", args.snapshot_id])
    if args.print_manifest:
        cmd.append("--print-manifest")
    if args.dry_run:
        cmd.append("--dry-run")
    transport = getattr(args, "transport", "auto")
    if transport:
        cmd.extend(["--transport", transport])
    return cmd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--host", help="explicit container SSH host")
    parser.add_argument("--port", type=int, help="explicit container SSH port")
    parser.add_argument("--container-name", help="identity label for the prepared root")
    parser.add_argument("--context-file", help="VAWS task context; unused for live execution roots")
    parser.add_argument("--execution-id", help="refused: coordinator prepares managed sources")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--workspace-id", default=None)
    parser.add_argument("--runtime-root", default=None, help="prepared work root; default /vllm-workspace")
    parser.add_argument("--container-user", default=DEFAULT_CONTAINER_USER)
    parser.add_argument("--container-cache-root", default=DEFAULT_CONTAINER_CACHE_ROOT)
    parser.add_argument("--preserve-path", action="append", default=[])
    parser.add_argument("--snapshot-id", default=None)
    parser.add_argument("--print-manifest", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--transport",
        choices=TRANSFER_MODES,
        default="auto",
        help="auto prefers incremental Git push and falls back to the full-bundle transport.",
    )
    parser.add_argument("--print-derived-args", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        repo_root = repo_root_from(Path(args.repo_root))
        derived = build_derived_args(repo_root, args)
        low_level_cmd = build_low_level_command(derived, args)
        if args.print_derived_args:
            payload = dict(derived)
            payload["status"] = "ok"
            payload["command"] = low_level_cmd
            print_json(payload)
            return 0
        result = subprocess.run(low_level_cmd, capture_output=True, text=True)
        if result.stderr:
            sys.stderr.write(result.stderr)
            if not result.stderr.endswith("\n"):
                sys.stderr.write("\n")
        if result.stdout.strip():
            try:
                child = json.loads(result.stdout)
            except json.JSONDecodeError:
                child = {
                    "status": "failed",
                    "error": "parity engine returned non-JSON stdout",
                    "stdout_tail": result.stdout[-500:],
                }
            if isinstance(child, dict):
                print_json(child)
            else:
                print_json({"status": "failed", "error": "parity engine returned a non-object"})
        elif result.returncode != 0:
            print_json({
                "status": "failed",
                "error": f"parity engine exited {result.returncode} with empty stdout",
                "stderr_tail": (result.stderr or "")[-500:],
            })
        else:
            print_json({"status": "ok", "message": "parity engine produced no JSON"})
        return result.returncode
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
