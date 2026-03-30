import json

import yaml

from conftest import run_vaws


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def seed_overlay(vaws_repo, target_name="single-default"):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir(exist_ok=True)
    (overlay / "repos.yaml").write_text("", encoding="utf-8")
    (overlay / "auth.yaml").write_text("", encoding="utf-8")
    (overlay / "targets.yaml").write_text("", encoding="utf-8")
    (overlay / "state.json").write_text(
        json.dumps({"current_target": target_name}) + "\n",
        encoding="utf-8",
    )


def seed_session(vaws_repo, session_name):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir(exist_ok=True)
    (overlay / "state.json").write_text("{}\n", encoding="utf-8")
    session_dir = overlay / "sessions" / session_name
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "name": session_name,
                "workspace_root": "/vllm-workspace",
                "base_ref": "origin/main",
                "target": "single-default",
            }
        ),
        encoding="utf-8",
    )


def test_session_create_builds_manifest(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")
    result = run_vaws(vaws_repo, "session", "create", "feat_x")
    assert result.returncode == 0
    manifest = vaws_repo / ".workspace.local" / "sessions" / "feat_x" / "manifest.yaml"
    assert manifest.exists()


def test_session_switch_updates_current_pointer(vaws_repo):
    seed_session(vaws_repo, "feat_x")
    seed_session(vaws_repo, "feat_y")
    result = run_vaws(vaws_repo, "session", "switch", "feat_y")
    assert result.returncode == 0
    current = read_json(vaws_repo / ".workspace.local" / "state.json")
    assert current["current_session"] == "feat_y"


def test_session_switch_fails_when_missing(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")
    result = run_vaws(vaws_repo, "session", "switch", "missing")
    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "unknown" in output
    assert "session" in output


def test_session_status_prints_none_when_unset(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")
    result = run_vaws(vaws_repo, "session", "status")
    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "none" in output


def test_session_create_rejects_unsafe_session_name(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")
    result = run_vaws(vaws_repo, "session", "create", "../evil")
    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid session name" in output
    assert not (vaws_repo / ".workspace.local" / "evil" / "manifest.yaml").exists()


def test_session_create_fails_when_overlay_state_is_uninitialized(vaws_repo):
    result = run_vaws(vaws_repo, "session", "create", "feat_x")
    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "bootstrap" in output
    assert ".workspace.local" in output
    assert not (
        vaws_repo / ".workspace.local" / "sessions" / "feat_x" / "manifest.yaml"
    ).exists()


def test_session_create_uses_runtime_workspace_root_from_state(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")
    state_path = vaws_repo / ".workspace.local" / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "current_target": "single-default",
                "runtime": {"workspace_root": "/custom-workspace"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "session", "create", "feat_custom_root")

    assert result.returncode == 0
    manifest = read_yaml(
        vaws_repo
        / ".workspace.local"
        / "sessions"
        / "feat_custom_root"
        / "manifest.yaml"
    )
    assert manifest["workspace_root"] == "/custom-workspace"
