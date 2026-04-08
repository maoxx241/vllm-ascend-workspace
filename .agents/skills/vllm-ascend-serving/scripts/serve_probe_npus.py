#!/usr/bin/env python3
"""Probe NPU device availability on a workspace-managed remote host.

Runs ``npu-smi info`` on the **bare-metal host** (not the container) so that
processes from ALL containers are visible.  This avoids the PID-namespace
isolation issue where a container's npu-smi cannot see other containers'
workloads.

Usage:
    python3 serve_probe_npus.py --machine <alias>

Returns JSON with total devices, which are busy (with PIDs), and which are free.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import (
    emit_progress,
    host_endpoint,
    print_json,
    probe_npus,
    resolve_machine,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--machine", required=True, help="machine alias or host IP")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        record = resolve_machine(args.machine)
        alias = record["alias"]
        h_ep = host_endpoint(record)

        emit_progress("probe-npus", f"probing NPU devices on host {alias}")
        npu_info = probe_npus(h_ep)

        output = {
            "status": "ok",
            "machine": alias,
            **npu_info,
        }
        print_json(output)
        return 0

    except Exception as exc:
        print_json({
            "status": "failed",
            "error": str(exc),
            "machine": getattr(args, "machine", None),
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
