from __future__ import annotations

import json
from typing import Any


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def unexpected_subcommand(namespace: str, value: object) -> RuntimeError:
    return RuntimeError(f"unknown {namespace} command: {value}")
