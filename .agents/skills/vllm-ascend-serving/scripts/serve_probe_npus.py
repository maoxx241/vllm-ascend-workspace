#!/usr/bin/env python3
"""Host NPU occupancy diagnostic. Not allocation authority."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import print_json, ssh_exec  # noqa: E402
from vaws_remote_target import SshEndpoint, ssh_endpoint_from_mapping  # noqa: E402
from vaws_task_target import task_client  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--context-file")
    parser.add_argument("--execution-id")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--user", default="root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.host:
            host_ep = SshEndpoint(host=args.host, port=args.port, user=args.user)
        else:
            client = task_client(args.context_file)
            if not args.execution_id:
                print_json({"status": "needs_input", "error": "pass --host or --execution-id"})
                return 1
            observation = client.observe(args.execution_id, "status")
            target = observation.get("target") or {}
            host_ep = ssh_endpoint_from_mapping(target.get("host_endpoint") or target.get("endpoint"))
        from vaws_coordinator.host_queue import parse_npu_smi_info

        result = ssh_exec(host_ep, "npu-smi info", check=False)
        if result.returncode != 0:
            raise RuntimeError(f"npu-smi failed: {result.stderr[:500]}")
        parsed = parse_npu_smi_info(result.stdout)
        print_json({"status": "ok", "host": host_ep.to_dict(), "npu_info": parsed})
        return 0
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
