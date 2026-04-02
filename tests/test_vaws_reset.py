import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib import reset
from tools.lib.config import RepoPaths
from tools.lib.overlay import ensure_overlay_layout
from tools.lib.remote import CleanupResult
from tools.lib.runtime import read_state, write_state


def _set_remote(repo: Path, remote_name: str, url: str) -> None:
    subprocess.run(
        ["git", "remote", "set-url", remote_name, url],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )


def _seed_servers_inventory(paths: RepoPaths, server_name: str = "lab-a") -> None:
    paths.local_servers_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "servers": {
                    server_name: {
                        "host": "10.0.0.12",
                        "port": 22,
                        "login_user": "root",
                        "ssh_auth_ref": "shared-lab-a",
                        "status": "ready",
                        "runtime": {
                            "image_ref": "quay.nju.edu.cn/ascend/vllm-ascend:latest",
                            "container_name": "vaws-workspace",
                            "ssh_port": 41001,
                            "workspace_root": "/vllm-workspace",
                            "bootstrap_mode": "host-then-container",
                            "host_workspace_path": f"/root/.vaws/targets/{server_name}/workspace",
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _seed_reset_request(paths: RepoPaths, confirmation_id: str = "reset-1") -> None:
    paths.reset_request_file.write_text(
        json.dumps(
            {
                "confirmation_id": confirmation_id,
                "status": "pending",
                "confirmation_phrase": reset.CONFIRMATION_PHRASE,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_prepare_reset_records_current_server(vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    write_state(
        paths,
        {
            "schema_version": 2,
            "servers": {"lab-a": {}},
            "current_server": "lab-a",
            "current_session": "feature_x",
        },
    )

    assert reset.prepare_reset(paths) == 0

    request = json.loads(paths.reset_request_file.read_text(encoding="utf-8"))
    assert request["approved_server"] == "lab-a"
    assert "approved_target" not in request


def test_reset_execute_removes_retired_targets_file_and_current_selection(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    _seed_servers_inventory(paths)
    _seed_reset_request(paths)
    paths.local_targets_file.write_text("current_target: legacy-box\n", encoding="utf-8")
    write_state(
        paths,
        {
            "schema_version": 2,
            "servers": {"lab-a": {"container_access": {"status": "ready"}}},
            "current_server": "lab-a",
            "current_session": "feature_x",
        },
    )

    monkeypatch.setattr(
        reset,
        "cleanup_server_runtime",
        lambda *_args, **_kwargs: CleanupResult("lab-a", "removed", "runtime removed"),
    )
    monkeypatch.setattr(reset, "_run_known_hosts_cleanup", lambda *_args, **_kwargs: None)

    assert reset.execute_reset(paths, "reset-1", reset.CONFIRMATION_PHRASE) == 0

    state = read_state(paths)
    assert state == {"schema_version": 3, "servers": {}, "services": {}, "benchmark_runs": {}}
    assert not paths.local_targets_file.exists()
    assert not paths.reset_request_file.exists()


def test_reset_execute_fails_closed_on_invalid_servers_inventory(vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    _seed_reset_request(paths)
    write_state(
        paths,
        {
            "schema_version": 2,
            "servers": {},
            "current_server": "lab-a",
        },
    )
    paths.local_servers_file.write_text("not: [valid\n", encoding="utf-8")
    state_before = paths.local_state_file.read_text(encoding="utf-8")

    result = reset.execute_reset(paths, "reset-1", reset.CONFIRMATION_PHRASE)

    assert result == 1
    assert paths.local_state_file.read_text(encoding="utf-8") == state_before
    assert paths.reset_request_file.exists()


def test_reset_execute_uses_live_git_topology_not_repos_yaml(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    _seed_reset_request(paths)
    paths.local_repos_file.write_text("not: [valid\n", encoding="utf-8")

    _set_remote(vaws_repo, "origin", "https://github.com/example/updated-workspace.git")
    _set_remote(vaws_repo, "upstream", "https://github.com/example/public-workspace.git")

    monkeypatch.setattr(reset, "_run_known_hosts_cleanup", lambda *_args, **_kwargs: None)

    assert reset.execute_reset(paths, "reset-1", reset.CONFIRMATION_PHRASE) == 0

    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=vaws_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    upstream = subprocess.run(
        ["git", "remote", "get-url", "upstream"],
        cwd=vaws_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert origin == "https://github.com/example/updated-workspace.git"
    assert upstream == "https://github.com/example/public-workspace.git"
