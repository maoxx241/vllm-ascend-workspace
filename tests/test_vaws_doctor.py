from conftest import run_vaws


def test_doctor_reports_missing_overlay(vaws_repo):
    result = run_vaws(vaws_repo, "doctor")
    assert result.returncode == 1
    assert ".workspace.local" in result.stdout


def test_init_creates_overlay_files(vaws_repo):
    result = run_vaws(vaws_repo, "init")
    assert result.returncode == 0
    assert (vaws_repo / ".workspace.local" / "targets.yaml").exists()
    assert (vaws_repo / ".workspace.local" / "repos.yaml").exists()
    assert (vaws_repo / ".workspace.local" / "auth.yaml").exists()
    assert (vaws_repo / ".workspace.local" / "state.json").exists()
