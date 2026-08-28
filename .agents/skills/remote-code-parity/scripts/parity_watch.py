#!/usr/bin/env python3
"""Continuously publish content snapshots to staging; never change running code."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import signal
import threading
from pathlib import Path

import remote_code_parity as parity


def stage_once(args, previous: str | None):
    args.apply_mode = "source-only"
    args.print_manifest = False
    if args.force_reinstall or args.dry_run:
        raise ValueError("a staging watcher cannot rebuild/install or acknowledge a dry run")
    root = Path(args.workspace_root).resolve()
    fingerprint = parity.workspace_fingerprint(root, parity.parse_sources(args.source))
    if fingerprint == previous:
        return previous, None
    # A mutation during transport will differ on the next iteration. Do not
    # acknowledge a post-sync fingerprint that was never actually transferred.
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = parity.run_sync(args)
    result = json.loads(output.getvalue())
    if code:
        raise RuntimeError(json.dumps(result))
    return fingerprint, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    own, remaining = parser.parse_known_args()
    if remaining[:1] == ["--"]:
        remaining.pop(0)
    if own.interval < 0.1:
        parser.error("interval must be at least 0.1 seconds")
    args = parity.build_parser().parse_args(["sync", *remaining])
    stopped = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stopped.set())
    previous = None
    while not stopped.is_set():
        previous, result = stage_once(args, previous)
        if result is not None:
            print(json.dumps({"kind": "staged", **result}), flush=True)
        if own.once:
            break
        stopped.wait(own.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
