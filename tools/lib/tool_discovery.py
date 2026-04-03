from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .agent_contract import ALLOWED_ACTION_KINDS


DISCOVERY_ROOT = Path(".agents/discovery")


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid yaml object: {path}")
    return data


def load_discovery_index(root: Path) -> dict[str, Any]:
    return _load_yaml(root / DISCOVERY_ROOT / "index.yaml")


def validate_discovery_tree(root: Path) -> list[str]:
    errors: list[str] = []
    index = load_discovery_index(root)

    contract_path = root / str(index["contract_path"])
    if not contract_path.exists():
        errors.append(f"missing contract_path: {contract_path}")

    for family in index.get("families", []):
        manifest_path = root / str(family["path"])
        if not manifest_path.exists():
            errors.append(f"missing family manifest: {manifest_path}")
            continue
        manifest = _load_yaml(manifest_path)
        for tool in manifest.get("tools", []):
            action_kind = str(tool["action_kind"])
            if action_kind not in ALLOWED_ACTION_KINDS:
                errors.append(f"invalid action_kind for {tool['tool_id']}: {action_kind}")
            tool_path = root / str(tool["path"])
            if not tool_path.exists():
                errors.append(f"missing tool path: {tool_path}")
            if tool.get("side_effects") == []:
                errors.append(f"empty side_effects boilerplate for {tool['tool_id']}")
    return errors
