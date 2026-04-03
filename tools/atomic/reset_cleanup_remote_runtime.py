from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.lib.config import RepoPaths
from tools.lib.reset_cleanup import cleanup_remote_runtime


def cleanup_remote_runtime_tool(paths: RepoPaths, server_name: str) -> dict[str, object]:
    return cleanup_remote_runtime(paths, server_name)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="reset.cleanup_remote_runtime")
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args(argv)
    result = cleanup_remote_runtime_tool(RepoPaths(root=Path.cwd()), args.server_name)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
