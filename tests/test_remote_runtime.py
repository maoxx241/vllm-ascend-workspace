import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.config import RepoPaths
from tools.lib.remote import (
    _bootstrap_container_runtime,
    CredentialGroup,
    HostSpec,
    RuntimeSpec,
    TargetContext,
    VerificationResult,
    verify_runtime,
)


def _host_ctx() -> TargetContext:
    return TargetContext(
        name="lab-a",
        host=HostSpec(
            name="lab-a",
            host="10.0.0.12",
            port=22,
            login_user="root",
            auth_group="default-server-auth",
            ssh_auth_ref="default-server-auth",
        ),
        credential=CredentialGroup(
            mode="ssh-key",
            username="root",
            key_path="/tmp/id_rsa",
        ),
        runtime=RuntimeSpec(
            image_ref="registry.example.com/ascend/vllm-ascend:test",
            container_name="vaws-owner",
            ssh_port=63269,
            workspace_root="/vllm-workspace",
            bootstrap_mode="host-then-container",
            host_workspace_path="/root/.vaws/targets/lab-a/workspace",
            docker_run_args=[],
        ),
    )


def test_verify_runtime_reports_needs_repair_when_only_docker_exec_works(
    monkeypatch, tmp_path
):
    ctx = _host_ctx()

    def fake_probe_container_ssh(_ctx):
        return ("needs_repair", "connection refused")

    def fake_probe_docker_exec(_ctx):
        return ("ready", "docker exec ok")

    monkeypatch.setattr(
        "tools.lib.remote._probe_container_ssh_transport",
        fake_probe_container_ssh,
    )
    monkeypatch.setattr("tools.lib.remote._probe_docker_exec_transport", fake_probe_docker_exec)

    result = verify_runtime(RepoPaths(root=Path(tmp_path)), ctx)

    assert isinstance(result, VerificationResult)
    assert result.status == "needs_repair"
    assert result.runtime["transport"] == "container-ssh"
    assert result.runtime["container_endpoint"].startswith("ssh://root@")
    assert [check.name for check in result.checks] == ["container_ssh", "docker_exec"]


def test_container_bootstrap_writes_key_only_sshd_config(monkeypatch):
    ctx = _host_ctx()
    recorded = {}

    def fake_run_docker_exec(_ctx, script):
        recorded["script"] = script
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr("tools.lib.remote._run_docker_exec", fake_run_docker_exec)
    monkeypatch.setattr("tools.lib.remote._probe_container_ssh", lambda _ctx: True)

    transport = _bootstrap_container_runtime(ctx)

    assert transport == "container-ssh"
    script = recorded["script"]
    assert "Acquire::Check-Date=false" in script
    assert "openssh-server" in script
    assert "ssh-keygen -A" in script
    assert "PasswordAuthentication no" in script
    assert "KbdInteractiveAuthentication no" in script
    assert "PermitRootLogin prohibit-password" in script


def test_verify_runtime_does_not_claim_ready_when_all_host_transports_fail(
    monkeypatch, tmp_path
):
    ctx = _host_ctx()
    monkeypatch.setattr(
        "tools.lib.remote._probe_container_ssh_transport",
        lambda _ctx: ("needs_repair", "connection refused"),
    )
    monkeypatch.setattr(
        "tools.lib.remote._probe_docker_exec_transport",
        lambda _ctx: ("needs_repair", "container missing"),
    )

    result = verify_runtime(RepoPaths(root=Path(tmp_path)), ctx)

    assert result.status == "needs_repair"
    assert result.runtime["transport"] == "container-ssh"
    assert [check.status for check in result.checks] == ["needs_repair", "needs_repair"]
