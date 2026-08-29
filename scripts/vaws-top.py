#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys

from vaws_top_client import (
    ClientError, VawsTopClient, format_capacity, format_mounts, format_npu, format_server, format_servers,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="vaws-top", description="Compact cached NPU fleet status for agents")
    result.add_argument("--url", help="vaws-top loopback API URL (default: http://127.0.0.1:8789)")
    result.add_argument("--json", action="store_true", help="emit compact JSON")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("servers", help="list monitored servers")
    npu = sub.add_parser("npu", help="show cached NPU status by IP, hostname, name, or server id")
    npu.add_argument("host")
    npu.add_argument("--live", action="store_true", help="ask the collector for a fresh remote snapshot")
    npu.add_argument("--timeout", type=int, default=30)
    npu.add_argument("--processes", action="store_true", help="include compact process records in JSON")
    npu.add_argument("--process-details", action="store_true", help="include pwd and command in JSON")
    npu.add_argument("--max-age", type=int, help="exit 3 when the cached snapshot is older than this many seconds")
    npu.add_argument("--ultra-compact", action="store_true", help="emit one summary line")
    status = sub.add_parser("status", help="show NPU, CPU, memory, Docker, processes, and storage")
    status.add_argument("host")
    status.add_argument("--live", action="store_true")
    status.add_argument("--timeout", type=int, default=30)
    status.add_argument("--process-details", action="store_true")
    mounts = sub.add_parser("mounts", help="show mount points and likely model-weight storage")
    mounts.add_argument("host")
    mounts.add_argument("--live", action="store_true")
    mounts.add_argument("--timeout", type=int, default=30)
    capacity = sub.add_parser("capacity", help="find fresh cached servers with idle NPUs")
    capacity.add_argument("--min-idle", type=int, default=1)
    capacity.add_argument("--max-age", type=int, default=300)
    capacity.add_argument("--tag", action="append", default=[])
    capacity.add_argument("--include-disabled", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        client = VawsTopClient(args.url)
        if args.command == "servers":
            payload = client.servers()
            output = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if args.json else format_servers(payload)
        elif args.command == "npu":
            payload = client.npu(
                args.host, args.processes or args.process_details, args.process_details,
                "live" if args.live else "cache", args.timeout,
            )
            output = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if args.json else format_npu(payload, args.ultra_compact)
            age = payload.get("age_seconds")
            if args.max_age is not None and (age is None or age > args.max_age):
                print(output)
                print(f"stale snapshot: age={age} max={args.max_age}", file=sys.stderr)
                return 3
        elif args.command in ("status", "mounts"):
            payload = client.server(
                args.host, "live" if args.live else "cache", True,
                getattr(args, "process_details", False), args.timeout,
            )
            if args.json:
                output = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            else:
                output = format_server(payload) if args.command == "status" else format_mounts(payload)
        else:
            payload = client.capacity(args.min_idle, args.max_age, args.tag, args.include_disabled)
            output = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if args.json else format_capacity(payload)
        print(output)
        return 0
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
