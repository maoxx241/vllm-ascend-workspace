from pathlib import Path

import pytest
import yaml

from conftest import run_vaws


def test_remotes_normalize_prefers_main(vaws_repo):
    result = run_vaws(vaws_repo, "remotes", "normalize")
    assert result.returncode == 0
    assert "origin/main" in result.stdout


def test_remotes_normalize_uses_overlay_workspace_default_branch(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "repos.yaml").write_text(
        yaml.safe_dump(
            {
                "workspace": {
                    "default_branch": "release",
                    "push_remote": "upstream",
                }
            }
        ),
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "remotes", "normalize")

    assert result.returncode == 0
    assert "upstream/release" in result.stdout


@pytest.mark.parametrize(
    ("contents", "expected_message"),
    [
        ("workspace: [1, 2\n", "invalid"),
        ("workspace: [\n", "invalid"),
    ],
)
def test_remotes_normalize_fails_for_corrupted_repos_config(
    vaws_repo, contents, expected_message
):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "repos.yaml").write_text(contents, encoding="utf-8")

    result = run_vaws(vaws_repo, "remotes", "normalize")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert expected_message in output
    assert "repos.yaml" in output
    assert "origin/main" not in output


def test_remotes_normalize_fails_for_invalid_utf8_repos_config(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "repos.yaml").write_bytes(b"\xff")

    result = run_vaws(vaws_repo, "remotes", "normalize")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid" in output
    assert "repos.yaml" in output
    assert "origin/main" not in output


def test_sync_wrapper_calls_python_entrypoint(repo_root):
    sync_text = (repo_root / "sync").read_text(encoding="utf-8")
    setup_text = (repo_root / "setup").read_text(encoding="utf-8")
    normalized_sync = sync_text.replace('"', "")
    normalized_setup = setup_text.replace('"', "")
    assert "tools/vaws.py sync" in normalized_sync
    assert "tools/vaws.py init" in normalized_setup


def test_sync_command_returns_success(vaws_repo):
    result = run_vaws(vaws_repo, "sync")
    assert result.returncode == 0
    assert "sync" in result.stdout.lower()


def test_setup_and_sync_wrappers_are_executable(repo_root):
    assert (repo_root / "setup").is_file()
    assert (repo_root / "sync").is_file()
    assert (repo_root / "setup").stat().st_mode & 0o111
    assert (repo_root / "sync").stat().st_mode & 0o111


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
