import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.code_parity import CodeParityResult
from tools.lib.config import RepoPaths
from tools.lib.remote_types import CredentialGroup, HostSpec, RemoteError, RuntimeSpec, TargetContext
from tools.lib.repo_targets import resolve_repo_targets
from tools.lib.runtime_env import _repo_reinstall_required, ensure_runtime_environment


def _generic_state_file(repo_root: Path) -> Path:
    return repo_root / ".workspace.local" / "state.json"


def _parity_result(status: str, summary: str) -> CodeParityResult:
    return CodeParityResult(
        status=status,
        summary=summary,
        mismatches=[] if status == "ready" else ["workspace"],
        desired_state={},
        remote_state={},
    )


def test_runtime_env_module_does_not_persist_generic_state():
    source = (ROOT / "tools/lib/runtime_env.py").read_text(encoding="utf-8")
    assert "capability_state" not in source


def test_repo_reinstall_required_skips_python_only_changes(vaws_repo):
    repo_path = vaws_repo / "vllm-ascend"
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    (repo_path / "feature_only.py").write_text("print('ok')\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature_only.py"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "python only change"],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    )
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    assert _repo_reinstall_required(repo_path, before, after) is False


def test_repo_reinstall_required_detects_native_changes(vaws_repo):
    repo_path = vaws_repo / "vllm-ascend"
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    native_path = repo_path / "csrc" / "native_kernel.cpp"
    native_path.parent.mkdir(parents=True, exist_ok=True)
    native_path.write_text("// native\n", encoding="utf-8")
    subprocess.run(["git", "add", "csrc/native_kernel.cpp"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "native change"],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    )
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    assert _repo_reinstall_required(repo_path, before, after) is True


def test_ensure_runtime_environment_runs_first_install_commands(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    desired = resolve_repo_targets(
        paths,
        vllm_upstream_tag=None,
        vllm_ascend_upstream_branch="main",
    )
    captured = {}

    def fake_run_runtime_command(ctx, transport, script):
        captured["transport"] = transport
        captured["script"] = script
        return subprocess.CompletedProcess(["bash"], 0, "", "")

    monkeypatch.setattr(
        "tools.lib.runtime_env.resolve_server_context",
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
                ssh_port=63269,
                workspace_root="/vllm-workspace",
                bootstrap_mode="host-then-container",
                host_workspace_path="/root/.vaws/targets/lab-a/workspace",
                docker_run_args=[],
            ),
        ),
    )
    monkeypatch.setattr(
        "tools.lib.runtime_env.resolve_available_runtime_transport",
        lambda _ctx: "container-ssh",
    )
    monkeypatch.setattr(
        "tools.lib.runtime_env.verify_code_parity",
        lambda *_args, **_kwargs: _parity_result(
            "needs_repair",
            "remote code parity mismatch for lab-a: workspace",
        ),
    )
    monkeypatch.setattr("tools.lib.runtime_env.run_container_command", fake_run_runtime_command)

    result = ensure_runtime_environment(paths, "lab-a", desired)

    assert result.status == "ready"
    assert captured["transport"] == "container-ssh"
    assert "pip uninstall -y vllm vllm-ascend || true" in captured["script"]
    assert "export VLLM_TARGET_DEVICE=empty" in captured["script"]
    assert "pip install -e . --no-build-isolation" in captured["script"]
    assert "pip install -r requirements.txt" in captured["script"]
    assert "pip install -v -e . --no-build-isolation" in captured["script"]
    assert not _generic_state_file(vaws_repo).exists()


def test_ensure_runtime_environment_shell_quotes_workspace_paths(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    desired = resolve_repo_targets(
        paths,
        vllm_upstream_tag=None,
        vllm_ascend_upstream_branch="main",
    )
    captured = {}
    workspace_root = "/tmp/work space; echo pwned"

    def fake_run_runtime_command(ctx, transport, script):
        captured["script"] = script
        return subprocess.CompletedProcess(["bash"], 0, "", "")

    monkeypatch.setattr(
        "tools.lib.runtime_env.resolve_server_context",
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
                ssh_port=63269,
                workspace_root=workspace_root,
                bootstrap_mode="host-then-container",
                host_workspace_path="/root/.vaws/targets/lab-a/workspace",
                docker_run_args=[],
            ),
        ),
    )
    monkeypatch.setattr(
        "tools.lib.runtime_env.resolve_available_runtime_transport",
        lambda _ctx: "container-ssh",
    )
    monkeypatch.setattr(
        "tools.lib.runtime_env.verify_code_parity",
        lambda *_args, **_kwargs: _parity_result(
            "needs_repair",
            "remote code parity mismatch for lab-a: workspace",
        ),
    )
    monkeypatch.setattr("tools.lib.runtime_env.run_container_command", fake_run_runtime_command)

    result = ensure_runtime_environment(paths, "lab-a", desired)

    assert result.status == "ready"
    assert f"cd {shlex.quote(f'{workspace_root}/workspace')}" in captured["script"]
    assert (
        "source "
        f"{shlex.quote(f'{workspace_root}/workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash')}"
        in captured["script"]
    )
    assert f"cd {shlex.quote(f'{workspace_root}/workspace/vllm')}" in captured["script"]
    assert f"cd {shlex.quote(f'{workspace_root}/workspace/vllm-ascend')}" in captured["script"]
    assert not _generic_state_file(vaws_repo).exists()


def test_ensure_runtime_environment_refuses_non_ssh_transport(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    desired = resolve_repo_targets(
        paths,
        vllm_upstream_tag=None,
        vllm_ascend_upstream_branch="main",
    )
    monkeypatch.setattr(
        "tools.lib.runtime_env.resolve_server_context",
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
                ssh_port=63269,
                workspace_root="/vllm-workspace",
                bootstrap_mode="host-then-container",
                host_workspace_path="/root/.vaws/targets/lab-a/workspace",
                docker_run_args=[],
            ),
        ),
    )
    monkeypatch.setattr(
        "tools.lib.runtime_env.resolve_available_runtime_transport",
        lambda _ctx: (_ for _ in ()).throw(RemoteError("container_ssh not ready for lab-a")),
    )
    monkeypatch.setattr(
        "tools.lib.runtime_env.verify_code_parity",
        lambda *_args, **_kwargs: _parity_result(
            "ready",
            "remote code parity ready for lab-a",
        ),
    )

    result = ensure_runtime_environment(paths, "lab-a", desired)

    assert result.status == "needs_repair"
    assert "container_ssh" in result.summary


def test_ensure_runtime_environment_uses_live_transport_without_cached_state(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    desired = resolve_repo_targets(
        paths,
        vllm_upstream_tag=None,
        vllm_ascend_upstream_branch="main",
    )
    captured = {}

    def fake_run_runtime_command(ctx, transport, script):
        captured["transport"] = transport
        captured["script"] = script
        return subprocess.CompletedProcess(["bash"], 0, "", "")

    monkeypatch.setattr(
        "tools.lib.runtime_env.resolve_server_context",
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
                ssh_port=63269,
                workspace_root="/vllm-workspace",
                bootstrap_mode="host-then-container",
                host_workspace_path="/root/.vaws/targets/lab-a/workspace",
                docker_run_args=[],
            ),
        ),
    )
    monkeypatch.setattr(
        "tools.lib.runtime_env.resolve_available_runtime_transport",
        lambda _ctx: "container-ssh",
        raising=False,
    )
    monkeypatch.setattr(
        "tools.lib.runtime_env.verify_code_parity",
        lambda *_args, **_kwargs: _parity_result(
            "needs_repair",
            "remote code parity mismatch for lab-a: workspace",
        ),
    )
    monkeypatch.setattr("tools.lib.runtime_env.run_container_command", fake_run_runtime_command)

    result = ensure_runtime_environment(paths, "lab-a", desired)

    if result.status != "ready":
        pytest.fail(f"live runtime transport should bypass cached state gate, got: {result.status} / {result.summary}")
    assert captured["transport"] == "container-ssh"
    assert not _generic_state_file(vaws_repo).exists()
