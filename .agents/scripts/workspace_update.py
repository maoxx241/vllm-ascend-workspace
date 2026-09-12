#!/usr/bin/env python3
"""Check or apply stable VAWS releases; watch polls at most once per interval."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))

from vaws_workspace_update import Deferred, WorkspaceUpdater, redact, update_lock, watch
from vaws_venv import configure_windows_stdio


def main(argv=None) -> int:
    configure_windows_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("apply")
    watcher = sub.add_parser("watch")
    watcher.add_argument("--interval", type=float, default=300)
    watcher.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    emit = lambda value: print(value, flush=True)
    try:
        if args.command == "watch":
            if args.interval <= 0:
                parser.error("--interval must be positive")
            watch(args.root.resolve(), interval=args.interval, once=args.once, emit=emit)
        else:
            with update_lock(args.root):
                result = WorkspaceUpdater(args.root).step(apply=args.command == "apply")
            emit(json.dumps(result, ensure_ascii=False))
    except Deferred as exc:
        emit(json.dumps({"status": exc.status, "reason": exc.reason}))
    except KeyboardInterrupt:
        emit(json.dumps({"status": "stopped"}))
    except (OSError, ValueError, RuntimeError) as exc:
        emit(json.dumps({"status": "deferred", "reason": "operation_pending", "error_type": type(exc).__name__,
                         "detail": redact(str(exc))[-1500:]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
