from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _family_tool_ids(family_name: str) -> list[str]:
    manifest_path = ROOT / f".agents/discovery/families/{family_name}.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    return [tool["tool_id"] for tool in manifest["tools"]]


def test_workspace_diagnostics_family_lists_overlay_and_workspace_tools():
    assert _family_tool_ids("workspace-diagnostics") == [
        "workspace.diagnose_overlay",
        "workspace.diagnose_workspace",
    ]


def test_doctor_uses_workspace_diagnostics_not_capability_state():
    source = (ROOT / "tools/lib/doctor.py").read_text(encoding="utf-8")
    assert "capability_state" not in source
    assert "workspace_diagnostics" in source
