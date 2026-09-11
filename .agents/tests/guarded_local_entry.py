"""Test-only guard: deny network and remote-dev/MCP imports.

Usable as a sitecustomize module (survives `os.execve` of the same
interpreter) or as `python guarded_local_entry.py <script> ...`.
"""
from __future__ import annotations

import atexit
import importlib.abc
import json
import os
import sys
from pathlib import Path

_report = os.environ.get("ACCEPTANCE_GUARD_REPORT")
events: list[dict] = []


def audit(event, args):
    if event == "socket.getaddrinfo":
        events.append({"kind": "network_attempt", "event": event})
        raise RuntimeError("local-only acceptance forbids network")
    if event in ("socket.connect", "socket.bind"):
        address = args[1] if len(args) > 1 else None
        if isinstance(address, (tuple, list)):
            # Native Windows coordinator IPC is authenticated loopback TCP.
            # Keep DNS and every non-loopback address denied.
            if os.name == "nt" and address[0] == "127.0.0.1":
                return
            events.append({"kind": "network_attempt", "event": event})
            raise RuntimeError("local-only acceptance forbids network")


class NoForeignProvider(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "core" or fullname.startswith("core.") or fullname == "mcp" or fullname.startswith("mcp."):
            events.append({"kind": "forbidden_import", "module": fullname})
            raise ImportError("local task entry must not import an external provider or MCP SDK")


if _report:
    sys.addaudithook(audit)
    sys.meta_path.insert(0, NoForeignProvider())
    atexit.register(lambda: Path(_report).write_text(json.dumps({"events": events, "pid": os.getpid()})))


if __name__ == "__main__":
    import runpy

    entry = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_path(entry, run_name="__main__")
