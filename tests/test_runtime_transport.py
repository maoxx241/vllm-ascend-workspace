from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.remote_types import CredentialGroup, HostSpec, RuntimeSpec, TargetContext
from tools.lib.runtime_transport import (
    bootstrap_container_runtime,
    probe_container_ssh_transport,
    resolve_available_runtime_transport,
    run_detached_container_command,
)


def _ctx() -> TargetContext:
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
        credential=CredentialGroup(mode="ssh-key", username="root", key_path="/tmp/id_rsa"),
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


def test_probe_container_ssh_transport_reports_ready(monkeypatch):
    monkeypatch.setattr(
        "tools.lib.runtime_transport.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["ssh"], 0, "", ""),
    )

    assert probe_container_ssh_transport(_ctx()) == ("ready", "container SSH probe succeeded")


def test_bootstrap_container_runtime_returns_container_ssh_when_probe_succeeds(monkeypatch):
    recorded: dict[str, str] = {}
    monkeypatch.setattr(
        "tools.lib.runtime_transport.find_public_key_path",
        lambda: Path("/tmp/id_rsa.pub"),
    )
    monkeypatch.setattr(
        "pathlib.Path.read_text",
        lambda self, encoding="utf-8": "ssh-ed25519 AAAA test",
    )

    def fake_run_docker_exec(_ctx, script):
        recorded["script"] = script
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr("tools.lib.runtime_transport.run_docker_exec", fake_run_docker_exec)
    monkeypatch.setattr("tools.lib.runtime_transport.probe_container_ssh", lambda _ctx: True)

    assert bootstrap_container_runtime(_ctx()) == "container-ssh"
    assert "PasswordAuthentication no" in recorded["script"]


def test_resolve_available_runtime_transport_prefers_container_ssh(monkeypatch):
    monkeypatch.setattr(
        "tools.lib.runtime_transport.probe_container_ssh_transport",
        lambda _ctx: ("ready", "container SSH probe succeeded"),
    )
    monkeypatch.setattr(
        "tools.lib.runtime_transport.probe_docker_exec_transport",
        lambda _ctx: ("ready", "docker exec probe succeeded"),
    )

    assert resolve_available_runtime_transport(_ctx()) == "container-ssh"


def test_resolve_available_runtime_transport_falls_back_to_docker_exec(monkeypatch):
    monkeypatch.setattr(
        "tools.lib.runtime_transport.probe_container_ssh_transport",
        lambda _ctx: ("needs_repair", "container SSH probe failed"),
    )
    monkeypatch.setattr(
        "tools.lib.runtime_transport.probe_docker_exec_transport",
        lambda _ctx: ("ready", "docker exec probe succeeded"),
    )

    assert resolve_available_runtime_transport(_ctx()) == "docker-exec"


def test_run_detached_container_command_wraps_pid_and_log_paths(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run_container_command(_ctx, transport, script):
        captured["transport"] = transport
        captured["script"] = script
        return subprocess.CompletedProcess(["ssh"], 0, "4321\n", "")

    monkeypatch.setattr("tools.lib.runtime_transport.run_container_command", fake_run_container_command)

    result = run_detached_container_command(
        _ctx(),
        "container-ssh",
        "python serve.py",
        log_path="/tmp/svc.log",
        pid_path="/tmp/svc.pid",
    )

    assert result.stdout == "4321\n"
    assert captured["transport"] == "container-ssh"
    assert "nohup bash -lc" in str(captured["script"])
    assert "/tmp/svc.log" in str(captured["script"])
    assert "/tmp/svc.pid" in str(captured["script"])
