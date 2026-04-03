from __future__ import annotations

import json
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


def test_reset_cleanup_family_lists_destructive_tools_explicitly():
    assert _family_tool_ids("reset-cleanup") == [
        "reset.prepare_request",
        "reset.cleanup_remote_runtime",
        "reset.cleanup_overlay",
        "reset.cleanup_known_hosts",
        "reset.restore_public_remotes",
    ]


def test_reset_prepare_request_records_pending_confirmation(vaws_repo):
    from tools.atomic.reset_prepare_request import prepare_reset_request_tool

    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)

    result = prepare_reset_request_tool(paths)

    payload = json.loads(paths.reset_request_file.read_text(encoding="utf-8"))
    assert result["status"] == "ready"
    assert payload["status"] == "pending"
    assert payload["confirmation_phrase"]


def test_reset_cleanup_overlay_resets_local_overlay_files(vaws_repo):
    from tools.atomic.reset_cleanup_overlay import cleanup_overlay_tool

    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    (paths.local_overlay / "state.json").write_text('{"legacy": true}\n', encoding="utf-8")
    legacy_targets = paths.local_overlay / "targets.yaml"
    legacy_targets.write_text("current_target: legacy-box\n", encoding="utf-8")
    paths.reset_request_file.write_text('{"confirmation_id":"old"}\n', encoding="utf-8")
    paths.local_benchmark_runs_dir.mkdir(parents=True, exist_ok=True)
    (paths.local_benchmark_runs_dir / "run-001.json").write_text('{"summary":"old"}\n', encoding="utf-8")
    paths.local_servers_file.write_text("version: 1\nservers:\n  lab-a: {}\n", encoding="utf-8")

    result = cleanup_overlay_tool(paths)

    assert result["status"] == "ready"
    assert not (paths.local_overlay / "state.json").exists()
    assert not legacy_targets.exists()
    assert not paths.reset_request_file.exists()
    assert not paths.local_benchmark_runs_dir.exists()
    assert yaml.safe_load(paths.local_servers_file.read_text(encoding="utf-8")) == {
        "version": 1,
        "servers": {},
    }
