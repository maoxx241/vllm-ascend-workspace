import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.capability_state import (  # type: ignore[attr-defined]
    diagnose_state_residue,
    read_capability_state,
    write_capability_leaf,
)
from tools.lib.config import RepoPaths
from tools.lib.overlay import ensure_overlay_layout
from tools.lib.runtime import read_state


def test_write_capability_leaf_preserves_unrelated_state(tmp_path):
    paths = RepoPaths(root=tmp_path)
    ensure_overlay_layout(paths)
    write_capability_leaf(
        paths,
        ("git_auth",),
        {
            "status": "ready",
            "provider": "github-cli",
            "observed_at": "2026-04-01T12:00:00Z",
            "detail": "seed",
            "evidence_source": "migration",
        },
    )
    write_capability_leaf(
        paths,
        ("servers", "lab-a", "host_access"),
        {
            "status": "ready",
            "mode": "ssh-key",
            "observed_at": "2026-04-01T12:01:00Z",
            "detail": "ssh probe ok",
            "evidence_source": "machine-management",
        },
    )

    state = read_capability_state(paths)
    assert state["git_auth"]["provider"] == "github-cli"
    assert state["servers"]["lab-a"]["host_access"]["status"] == "ready"


def test_read_capability_state_rejects_retired_top_level_keys(tmp_path):
    paths = RepoPaths(root=tmp_path)
    ensure_overlay_layout(paths)
    paths.local_state_file.write_text(
        '{"schema_version": 2, "current_target": "legacy-box"}',
        encoding="utf-8",
    )

    try:
        read_capability_state(paths)
    except RuntimeError as exc:
        assert "retired state key present: current_target" in str(exc)
    else:
        raise AssertionError("retired state key should fail closed")


def test_read_state_returns_schema_v2_shell_when_state_missing(tmp_path):
    paths = RepoPaths(root=tmp_path)

    state = read_state(paths)

    assert state == {"schema_version": 2, "servers": {}}


def test_diagnose_state_residue_reports_retired_keys_without_raising(tmp_path):
    paths = RepoPaths(root=tmp_path)
    ensure_overlay_layout(paths)
    paths.local_state_file.write_text(
        '{"schema_version": 1, "lifecycle": {"foundation": {"status": "ready"}}, "current_target": "legacy-box"}\n',
        encoding="utf-8",
    )

    residue = diagnose_state_residue(paths)

    assert "state.json has unsupported schema_version: 1" in residue
    assert "state.json contains retired key: lifecycle" in residue
    assert "state.json contains retired key: current_target" in residue
