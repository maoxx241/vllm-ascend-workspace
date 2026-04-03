from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.config import RepoPaths
from tools.lib.overlay import ensure_overlay_layout


def _family_tool_ids(family_name: str) -> list[str]:
    manifest_path = ROOT / f".agents/discovery/families/{family_name}.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    return [tool["tool_id"] for tool in manifest["tools"]]


def test_workspace_foundation_family_lists_probe_first_tools():
    assert _family_tool_ids("workspace-foundation") == [
        "workspace.probe_config_validity",
        "workspace.probe_git_auth",
        "workspace.probe_repo_topology",
        "workspace.probe_submodules",
        "workspace.describe_repo_targets",
    ]


def test_workspace_probe_config_validity_reports_missing_overlay_files(vaws_repo):
    from tools.atomic.workspace_probe_config_validity import probe_config_validity

    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    paths.local_auth_file.unlink(missing_ok=True)

    result = probe_config_validity(paths)

    assert result["status"] == "needs_repair"
    assert any(".workspace.local/auth.yaml" in observation for observation in result["observations"])


def test_workspace_describe_repo_targets_returns_workspace_vllm_and_vllm_ascend(vaws_repo):
    from tools.atomic.workspace_describe_repo_targets import describe_repo_targets_tool

    result = describe_repo_targets_tool(RepoPaths(root=vaws_repo))

    assert result["status"] == "ready"
    payload = result["payload"]
    assert set(payload) == {"workspace", "vllm", "vllm_ascend"}
    assert payload["workspace"]["repo_name"] == "workspace"
    assert payload["vllm"]["repo_name"] == "vllm"
    assert payload["vllm_ascend"]["repo_name"] == "vllm-ascend"
