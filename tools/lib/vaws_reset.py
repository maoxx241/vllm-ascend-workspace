from __future__ import annotations

from typing import Any

from .config import RepoPaths
from .reset_cleanup import prepare_reset_request
from .vaws_compat import emit_json
from .vaws_compat import unexpected_subcommand


def run(paths: RepoPaths, args: Any) -> int:
    if args.reset_command == "prepare":
        result = prepare_reset_request(paths)
        emit_json(result)
        return 0 if result["status"] == "ready" else 1
    raise unexpected_subcommand("reset", args.reset_command)
