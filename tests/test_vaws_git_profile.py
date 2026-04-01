import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conftest import run_vaws, seed_overlay_files


def _remote_url(repo: Path, relative_path: str, remote_name: str) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", remote_name],
        cwd=repo / relative_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _set_remote_url(repo: Path, relative_path: str, remote_name: str, url: str) -> None:
    result = subprocess.run(
        ["git", "remote", "set-url", remote_name, url],
        cwd=repo / relative_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _write_auth_overlay(vaws_repo: Path) -> None:
    overlay = vaws_repo / ".workspace.local"
    (overlay / "auth.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "ssh_auth": {
                    "refs": {
                        "legacy-server-auth": {
                            "kind": "ssh-key",
                            "username": "root",
                        }
                    }
                },
                "git_auth": {
                    "refs": {
                        "legacy-git-auth": {
                            "kind": "ssh-agent",
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_ready_repos_overlay(vaws_repo: Path) -> None:
    overlay = vaws_repo / ".workspace.local"
    (overlay / "repos.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "workspace": {
                    "path": ".",
                    "default_branch": "main",
                    "protected_branches": ["main"],
                    "push_remote": "origin",
                    "upstream_remote": "upstream",
                },
                "submodules": {
                    "vllm": {
                        "path": "vllm",
                        "default_branch": "main",
                        "push_remote": "origin",
                        "upstream_remote": "upstream",
                        "upstream_url": "https://github.com/vllm-project/vllm.git",
                        "origin_url": "git@github.com:alice/vllm.git",
                    },
                    "vllm-ascend": {
                        "path": "vllm-ascend",
                        "default_branch": "main",
                        "push_remote": "origin",
                        "upstream_remote": "upstream",
                        "upstream_url": "https://github.com/vllm-project/vllm-ascend.git",
                        "origin_url": "git@github.com:alice/vllm-ascend.git",
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _set_remote_url(vaws_repo, "vllm", "origin", "git@github.com:alice/vllm.git")
    _set_remote_url(
        vaws_repo,
        "vllm-ascend",
        "origin",
        "git@github.com:alice/vllm-ascend.git",
    )


def _write_optional_vllm_origin_repos_overlay(vaws_repo: Path) -> None:
    overlay = vaws_repo / ".workspace.local"
    (overlay / "repos.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "workspace": {
                    "path": ".",
                    "default_branch": "main",
                    "protected_branches": ["main"],
                    "push_remote": "origin",
                    "upstream_remote": "upstream",
                },
                "submodules": {
                    "vllm": {
                        "path": "vllm",
                        "default_branch": "main",
                        "push_remote": "origin",
                        "upstream_remote": "upstream",
                        "upstream_url": "https://github.com/vllm-project/vllm.git",
                    },
                    "vllm-ascend": {
                        "path": "vllm-ascend",
                        "default_branch": "main",
                        "push_remote": "origin",
                        "upstream_remote": "upstream",
                        "upstream_url": "https://github.com/vllm-project/vllm-ascend.git",
                        "origin_url": "git@github.com:alice/vllm-ascend.git",
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_community_origin_repos_overlay(vaws_repo: Path) -> None:
    overlay = vaws_repo / ".workspace.local"
    (overlay / "repos.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "workspace": {
                    "path": ".",
                    "default_branch": "main",
                    "protected_branches": ["main"],
                    "push_remote": "origin",
                    "upstream_remote": "upstream",
                },
                "submodules": {
                    "vllm": {
                        "path": "vllm",
                        "default_branch": "main",
                        "push_remote": "origin",
                        "upstream_remote": "upstream",
                        "upstream_url": "https://github.com/vllm-project/vllm.git",
                    },
                    "vllm-ascend": {
                        "path": "vllm-ascend",
                        "default_branch": "main",
                        "push_remote": "origin",
                        "upstream_remote": "upstream",
                        "upstream_url": "https://github.com/vllm-project/vllm-ascend.git",
                        "origin_url": "https://github.com/vllm-project/vllm-ascend.git",
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_git_profile_reports_ready_for_existing_topology(vaws_repo):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    _write_ready_repos_overlay(vaws_repo)

    before = (vaws_repo / ".workspace.local" / "auth.yaml").read_text(encoding="utf-8")

    result = run_vaws(vaws_repo, "git-profile")

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "git-profile: ready" in output

    auth_after = (vaws_repo / ".workspace.local" / "auth.yaml").read_text(encoding="utf-8")
    assert auth_after != before
    auth = yaml.safe_load(auth_after)
    assert auth["ssh_auth"]["refs"]["legacy-server-auth"]["kind"] == "ssh-key"
    assert auth["git_auth"]["refs"]["legacy-git-auth"]["kind"] == "ssh-agent"
    assert auth["git_auth"]["refs"]["default-git-auth"]["kind"] == "ssh-agent"

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["lifecycle"]["git_profile"]["status"] == "ready"


def test_git_profile_rejects_community_origin_topology(vaws_repo):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    _write_community_origin_repos_overlay(vaws_repo)

    result = run_vaws(vaws_repo, "git-profile")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "needs_input" in output
    assert "vllm-ascend" in output

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["lifecycle"]["git_profile"]["status"] == "needs_input"


def test_git_profile_needs_input_without_vllm_ascend_origin(vaws_repo):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)

    result = run_vaws(
        vaws_repo,
        "git-profile",
        "--vllm-origin-url",
        "git@github.com:alice/vllm.git",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "needs_input" in output
    assert "vllm-ascend" in output

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["lifecycle"]["git_profile"]["status"] == "needs_input"

    repos = yaml.safe_load((vaws_repo / ".workspace.local" / "repos.yaml").read_text())
    assert repos["submodules"] == {}


def test_git_profile_needs_input_when_run_without_personalized_origin(vaws_repo):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)

    result = run_vaws(vaws_repo, "git-profile")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "needs_input" in output
    assert "vllm-ascend" in output

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["lifecycle"]["git_profile"]["status"] == "needs_input"

    repos = yaml.safe_load((vaws_repo / ".workspace.local" / "repos.yaml").read_text())
    assert repos["submodules"] == {}


def test_git_profile_writes_personalized_topology_and_preserves_ssh_auth_namespace(vaws_repo):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)

    result = run_vaws(
        vaws_repo,
        "git-profile",
        "--vllm-origin-url",
        "git@github.com:alice/vllm.git",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "git-profile: ready" in output

    repos = yaml.safe_load((vaws_repo / ".workspace.local" / "repos.yaml").read_text())
    assert repos["submodules"]["vllm"]["origin_url"] == "git@github.com:alice/vllm.git"
    assert repos["submodules"]["vllm"]["upstream_url"] == "https://github.com/vllm-project/vllm.git"
    assert repos["submodules"]["vllm-ascend"]["origin_url"] == "git@github.com:alice/vllm-ascend.git"
    assert repos["submodules"]["vllm-ascend"]["upstream_url"] == "https://github.com/vllm-project/vllm-ascend.git"

    assert _remote_url(vaws_repo, "vllm", "origin") == "git@github.com:alice/vllm.git"
    assert _remote_url(vaws_repo, "vllm", "upstream") == "https://github.com/vllm-project/vllm.git"
    assert _remote_url(vaws_repo, "vllm-ascend", "origin") == "git@github.com:alice/vllm-ascend.git"
    assert _remote_url(vaws_repo, "vllm-ascend", "upstream") == "https://github.com/vllm-project/vllm-ascend.git"

    auth = yaml.safe_load((vaws_repo / ".workspace.local" / "auth.yaml").read_text())
    assert "legacy-server-auth" in auth["ssh_auth"]["refs"]
    assert auth["ssh_auth"]["refs"]["legacy-server-auth"]["kind"] == "ssh-key"
    assert "legacy-git-auth" in auth["git_auth"]["refs"]
    assert auth["git_auth"]["refs"]["default-git-auth"]["kind"] == "ssh-agent"

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["lifecycle"]["git_profile"]["status"] == "ready"


def test_git_profile_accepts_only_vllm_ascend_origin_without_inventing_vllm_origin(vaws_repo):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    _set_remote_url(
        vaws_repo,
        "vllm",
        "origin",
        "git@github.com:alice/custom-vllm.git",
    )

    result = run_vaws(
        vaws_repo,
        "git-profile",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "git-profile: ready" in output

    repos = yaml.safe_load((vaws_repo / ".workspace.local" / "repos.yaml").read_text())
    assert repos["submodules"]["vllm"]["upstream_url"] == "https://github.com/vllm-project/vllm.git"
    assert "origin_url" not in repos["submodules"]["vllm"]
    assert repos["submodules"]["vllm-ascend"]["origin_url"] == "git@github.com:alice/vllm-ascend.git"

    assert _remote_url(vaws_repo, "vllm", "origin") == "git@github.com:alice/custom-vllm.git"
    assert _remote_url(vaws_repo, "vllm", "upstream") == "https://github.com/vllm-project/vllm.git"
    assert _remote_url(vaws_repo, "vllm-ascend", "origin") == "git@github.com:alice/vllm-ascend.git"


def test_git_profile_reports_ready_for_optional_vllm_origin_topology(vaws_repo):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    _set_remote_url(
        vaws_repo,
        "vllm",
        "origin",
        "git@github.com:alice/custom-vllm.git",
    )
    _set_remote_url(
        vaws_repo,
        "vllm-ascend",
        "origin",
        "git@github.com:alice/vllm-ascend.git",
    )
    _write_optional_vllm_origin_repos_overlay(vaws_repo)

    result = run_vaws(vaws_repo, "git-profile")

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "git-profile: ready" in output

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["lifecycle"]["git_profile"]["status"] == "ready"
    assert _remote_url(vaws_repo, "vllm", "origin") == "git@github.com:alice/custom-vllm.git"
    assert _remote_url(vaws_repo, "vllm", "upstream") == "https://github.com/vllm-project/vllm.git"


def test_git_profile_heals_default_git_auth_on_ready_path(vaws_repo):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    _write_ready_repos_overlay(vaws_repo)

    auth_path = vaws_repo / ".workspace.local" / "auth.yaml"
    auth = yaml.safe_load(auth_path.read_text())
    auth["git_auth"]["refs"].pop("default-git-auth", None)
    auth_path.write_text(yaml.safe_dump(auth, sort_keys=False), encoding="utf-8")

    result = run_vaws(vaws_repo, "git-profile")

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "git-profile: ready" in output

    healed_auth = yaml.safe_load(auth_path.read_text())
    assert healed_auth["ssh_auth"]["refs"]["legacy-server-auth"]["kind"] == "ssh-key"
    assert healed_auth["git_auth"]["refs"]["legacy-git-auth"]["kind"] == "ssh-agent"
    assert healed_auth["git_auth"]["refs"]["default-git-auth"]["kind"] == "ssh-agent"


def test_git_profile_handles_malformed_state_file_without_traceback(vaws_repo):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    _write_ready_repos_overlay(vaws_repo)
    (vaws_repo / ".workspace.local" / "state.json").write_text("{", encoding="utf-8")

    result = run_vaws(vaws_repo, "git-profile")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid runtime state" in output
    assert "traceback" not in output
