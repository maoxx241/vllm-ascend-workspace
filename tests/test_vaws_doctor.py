import json
import shutil

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


def test_doctor_fails_when_required_overlay_files_are_missing(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "targets.yaml").write_text("", encoding="utf-8")

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert "missing overlay files" in output
    assert "repos.yaml" in output


def test_doctor_fails_when_state_json_is_invalid(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "targets.yaml").write_text("", encoding="utf-8")
    (overlay / "repos.yaml").write_text("", encoding="utf-8")
    (overlay / "auth.yaml").write_text("", encoding="utf-8")
    (overlay / "state.json").write_text("{not-json", encoding="utf-8")

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "state.json" in output
    assert "invalid" in output


def test_doctor_fails_when_state_json_is_not_utf8(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "targets.yaml").write_text("", encoding="utf-8")
    (overlay / "repos.yaml").write_text("", encoding="utf-8")
    (overlay / "auth.yaml").write_text("", encoding="utf-8")
    (overlay / "state.json").write_bytes(b"\xff\xfe")

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "state.json" in output
    assert "invalid" in output
    assert "traceback" not in output


def test_doctor_fails_when_recursive_submodule_is_missing(vaws_repo):
    result = run_vaws(vaws_repo, "init")
    assert result.returncode == 0
    catlass_root = vaws_repo / "vllm-ascend" / "csrc" / "third_party" / "catlass"
    git_marker = catlass_root / ".git"
    shutil.rmtree(git_marker)

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "submodule" in output
    assert "catlass" in output


def test_doctor_succeeds_for_initialized_overlay_and_recursive_submodules(vaws_repo):
    result = run_vaws(vaws_repo, "init")
    assert result.returncode == 0

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 0
    assert "doctor: ok" in result.stdout.lower()


def test_init_writes_json_parseable_state_file(vaws_repo):
    result = run_vaws(vaws_repo, "init")

    assert result.returncode == 0
    state_text = (vaws_repo / ".workspace.local" / "state.json").read_text(
        encoding="utf-8"
    )
    assert json.loads(state_text) == {}


def test_init_fails_cleanly_when_overlay_path_is_a_file(vaws_repo):
    (vaws_repo / ".workspace.local").write_text("not-a-directory", encoding="utf-8")

    result = run_vaws(vaws_repo, "init")

    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert ".workspace.local" in output
    assert "directory" in output.lower()
    assert "traceback" not in output.lower()
