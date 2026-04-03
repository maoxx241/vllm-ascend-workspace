import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.code_parity import (
    CodeParityResult,
    build_materialization_script,
    ensure_code_parity,
    verify_code_parity,
)
from tools.lib.config import RepoPaths
from tools.lib.remote_types import CredentialGroup, HostSpec, RuntimeSpec, TargetContext
from tools.lib.repo_targets import resolve_repo_targets


def _generic_state_file(repo_root: Path) -> Path:
    return repo_root / ".workspace.local" / "state.json"


def test_code_parity_module_does_not_persist_generic_state():
    source = (ROOT / "tools/lib/code_parity.py").read_text(encoding="utf-8")
    assert "capability_state" not in source


def test_verify_code_parity_detects_stale_remote_commits(vaws_repo, monkeypatch):
    subprocess.run(["git", "tag", "0.18.0"], cwd=vaws_repo / "vllm", check=True)
    desired = resolve_repo_targets(
        RepoPaths(root=vaws_repo),
        vllm_upstream_tag="0.18.0",
        vllm_ascend_upstream_branch="main",
    )

    monkeypatch.setattr(
        "tools.lib.code_parity._collect_remote_git_state",
        lambda *_args, **_kwargs: {
            "workspace": {"commit": "0" * 40},
            "vllm": {"commit": desired.vllm.commit},
            "vllm-ascend": {"commit": desired.vllm_ascend.commit},
        },
    )

    result = verify_code_parity(RepoPaths(root=vaws_repo), "lab-a", desired)

    assert result.status == "needs_repair"
    assert result.mismatches == ["workspace"]


def test_build_materialization_script_uses_git_only(vaws_repo):
    subprocess.run(["git", "tag", "0.18.0"], cwd=vaws_repo / "vllm", check=True)
    desired = resolve_repo_targets(
        RepoPaths(root=vaws_repo),
        vllm_upstream_tag="0.18.0",
        vllm_ascend_upstream_branch="main",
    )

    script = build_materialization_script(desired, workspace_root="/vllm-workspace")

    assert "git clone" not in script
    assert "fetch --all" not in script
    assert "submodule sync --recursive" not in script
    assert "submodule update --init --recursive" not in script
    assert "checkout --detach" in script
    assert "tar -cf -" not in script
    assert "scp " not in script


def test_ensure_code_parity_syncs_workspace_mirror_before_runtime_checkout(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    desired = resolve_repo_targets(
        paths,
        vllm_upstream_tag=None,
        vllm_ascend_upstream_branch="main",
    )
    calls = []
    monkeypatch.setattr(
        "tools.lib.code_parity.resolve_server_context",
        lambda _paths, _server_name: TargetContext(
            name="lab-a",
            host=HostSpec(
                name="lab-a",
                host="10.0.0.12",
                port=22,
                login_user="root",
                auth_group="default-server-auth",
                ssh_auth_ref="default-server-auth",
            ),
            credential=CredentialGroup(mode="ssh-key", username="root"),
            runtime=RuntimeSpec(
                image_ref="image",
                container_name="container",
                ssh_port=41001,
                workspace_root="/vllm-workspace",
                bootstrap_mode="host-then-container",
                host_workspace_path="/root/.vaws/targets/lab-a/workspace",
                docker_run_args=[],
            ),
        ),
    )
    monkeypatch.setattr(
        "tools.lib.code_parity.sync_workspace_mirror",
        lambda *_args, **_kwargs: calls.append("sync"),
    )
    monkeypatch.setattr(
        "tools.lib.code_parity.resolve_available_runtime_transport",
        lambda _ctx: "container-ssh",
    )
    monkeypatch.setattr(
        "tools.lib.code_parity.run_container_command",
        lambda *_args, **_kwargs: calls.append("runtime")
        or subprocess.CompletedProcess(["bash"], 0, "", ""),
    )
    monkeypatch.setattr(
        "tools.lib.code_parity.verify_code_parity",
        lambda *_args, **_kwargs: CodeParityResult(
            status="ready",
            summary="code parity ready for lab-a",
            mismatches=[],
            desired_state={},
            remote_state={},
        ),
    )

    result = ensure_code_parity(paths, "lab-a", desired)

    assert result.status == "ready"
    assert calls[:2] == ["sync", "runtime"]
    assert not _generic_state_file(vaws_repo).exists()


def test_ensure_code_parity_uses_live_transport_without_cached_state(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    desired = resolve_repo_targets(
        paths,
        vllm_upstream_tag=None,
        vllm_ascend_upstream_branch="main",
    )
    calls = []

    monkeypatch.setattr(
        "tools.lib.code_parity.resolve_server_context",
        lambda _paths, _server_name: TargetContext(
            name="lab-a",
            host=HostSpec(
                name="lab-a",
                host="10.0.0.12",
                port=22,
                login_user="root",
                auth_group="default-server-auth",
                ssh_auth_ref="default-server-auth",
            ),
            credential=CredentialGroup(mode="ssh-key", username="root"),
            runtime=RuntimeSpec(
                image_ref="image",
                container_name="container",
                ssh_port=41001,
                workspace_root="/vllm-workspace",
                bootstrap_mode="host-then-container",
                host_workspace_path="/root/.vaws/targets/lab-a/workspace",
                docker_run_args=[],
            ),
        ),
    )
    monkeypatch.setattr(
        "tools.lib.code_parity.sync_workspace_mirror",
        lambda *_args, **_kwargs: calls.append("sync"),
    )
    monkeypatch.setattr(
        "tools.lib.code_parity.resolve_available_runtime_transport",
        lambda _ctx: "container-ssh",
        raising=False,
    )
    monkeypatch.setattr(
        "tools.lib.code_parity.run_container_command",
        lambda *_args, **_kwargs: calls.append("runtime")
        or subprocess.CompletedProcess(["bash"], 0, "", ""),
    )
    monkeypatch.setattr(
        "tools.lib.code_parity.verify_code_parity",
        lambda *_args, **_kwargs: CodeParityResult(
            status="ready",
            summary="code parity ready for lab-a",
            mismatches=[],
            desired_state={},
            remote_state={},
        ),
    )

    try:
        result = ensure_code_parity(paths, "lab-a", desired)
    except RuntimeError as exc:
        pytest.fail(f"live runtime transport should bypass cached state gate, got: {exc}")

    assert result.status == "ready"
    assert calls[:2] == ["sync", "runtime"]
    assert not _generic_state_file(vaws_repo).exists()
