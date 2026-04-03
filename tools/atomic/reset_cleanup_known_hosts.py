from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.lib.reset_cleanup import cleanup_known_hosts


def cleanup_known_hosts_tool(host: str, port: int) -> dict[str, object]:
    return cleanup_known_hosts(host, port)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="reset.cleanup_known_hosts")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    result = cleanup_known_hosts_tool(args.host, args.port)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
