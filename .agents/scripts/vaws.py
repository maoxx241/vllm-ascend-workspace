#!/usr/bin/env python3
"""VAWS local context and task facade; the same operations are remote-dev MCP tools."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / ".agents/lib"), str(ROOT / ".remote-dev")]
from vaws_agent_session import AgentSessions, CLIENTS, load_context
from core.vaws_ops import vaws_call


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    attach = sub.add_parser("attach", help="Adapter entry: native root/resume, child, or explicit task association")
    attach.add_argument("--client", choices=sorted(CLIENTS), required=True)
    attach.add_argument("--native-session-id", required=True)
    attach.add_argument("--cwd", default=str(Path.cwd()))
    attach.add_argument("--parent-context", default="")
    attach.add_argument("--association", default="")
    attach.add_argument("--agent-id", default="")
    for name in ("session", "run", "execution", "finish"):
        child = sub.add_parser(name)
        child.add_argument("--context-file")
        child.add_argument("--json", default="{}", help="Additional structured tool arguments")
        if name == "run":
            child.add_argument("--request-id", required=True)
            child.add_argument("--command", required=True)
        if name == "execution":
            child.add_argument("--execution-id", required=True)
            child.add_argument("--action", choices=["status", "tail", "stop"])
    args = vars(parser.parse_args())
    operation = args.pop("operation")
    if operation == "attach":
        inherited = args["parent_context"] or args["association"]
        store = AgentSessions(Path(load_context(inherited)["state_dir"])) if inherited else AgentSessions()
        print(json.dumps(store.attach(**args), ensure_ascii=False))
        return 0
    extra = json.loads(args.pop("json"))
    # Unset argparse defaults (None) must not silently override --json keys:
    # `--json '{"action":"stop"}'` degraded to a status query otherwise.
    merged = {**extra, **{key: value for key, value in args.items() if value is not None}}
    result = vaws_call("vaws." + operation, merged)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["result"]["outcome"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
