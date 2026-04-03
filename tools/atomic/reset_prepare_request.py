from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.lib.config import RepoPaths
from tools.lib.reset_cleanup import prepare_reset_request


def prepare_reset_request_tool(paths: RepoPaths) -> dict[str, object]:
    return prepare_reset_request(paths)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="reset.prepare_request")
    parser.parse_args(argv)
    result = prepare_reset_request_tool(RepoPaths(root=Path.cwd()))
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
