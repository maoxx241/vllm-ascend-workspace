import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conftest import run_vaws, seed_overlay_files
from tools.lib import foundation
from tools.lib.config import RepoPaths
from tools.lib.preflight import PreflightReport


def test_vaws_foundation_persists_status_in_state(vaws_repo, monkeypatch, capsys):
    monkeypatch.setattr(
        foundation,
        "check_local_control_plane_deps",
        lambda: PreflightReport(
            status="ready",
            installed_required=("git", "ssh", "python3"),
            missing_required=(),
            installed_recommended=("gh",),
            missing_recommended=(),
        ),
    )

    result = foundation.run_foundation(RepoPaths(root=vaws_repo))

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    output = capsys.readouterr().out
    assert result == 0
    assert "foundation: ready" in output
    assert state["lifecycle"]["foundation"]["status"] == "ready"


def test_run_foundation_returns_nonzero_for_blocked_status(vaws_repo, monkeypatch, capsys):
    monkeypatch.setattr(
        foundation,
        "check_local_control_plane_deps",
        lambda: PreflightReport(
            status="blocked",
            installed_required=("git",),
            missing_required=("ssh", "python3"),
            installed_recommended=(),
            missing_recommended=("gh",),
        ),
    )

    result = foundation.run_foundation(RepoPaths(root=vaws_repo))

    output = capsys.readouterr().out
    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert result == 1
    assert "foundation: blocked" in output
    assert state["lifecycle"]["foundation"]["status"] == "blocked"


def test_vaws_foundation_cli_fails_cleanly_for_invalid_state(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    seed_overlay_files(vaws_repo)
    (overlay / "state.json").write_text("{not-json", encoding="utf-8")

    result = run_vaws(vaws_repo, "foundation")

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "invalid runtime state: .workspace.local/state.json" in output
    assert "traceback" not in output.lower()
