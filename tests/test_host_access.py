from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.host_access import ensure_host_ssh_access, run_host_command
from tools.lib.remote_types import CredentialGroup, HostSpec, RuntimeSpec, TargetContext


def _ctx(*, mode: str = "ssh-key", password: str | None = None) -> TargetContext:
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
            mode=mode,
            username="root",
            password=password,
            key_path="/tmp/id_rsa",
        ),
        runtime=RuntimeSpec(
            image_ref="image",
            container_name="container",
            ssh_port=63269,
            workspace_root="/vllm-workspace",
            bootstrap_mode="host-then-container",
            host_workspace_path="/root/.vaws/targets/lab-a/workspace",
            docker_run_args=[],
        ),
    )


def test_ensure_host_ssh_access_returns_false_when_key_login_already_works(monkeypatch):
    monkeypatch.setattr(
        "tools.lib.host_access.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["ssh"], 0, "", ""),
    )

    assert ensure_host_ssh_access(_ctx(), allow_password_bootstrap=False) is False


def test_ensure_host_ssh_access_bootstraps_password_once_when_allowed(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "tools.lib.host_access.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["ssh"], 255, "", "no auth"),
    )
    monkeypatch.setattr(
        "tools.lib.host_access.install_local_public_key_on_host",
        lambda ctx: calls.append("install"),
    )
    monkeypatch.setattr(
        "tools.lib.host_access.verify_host_ssh_key_login",
        lambda ctx: calls.append("verify"),
    )

    assert ensure_host_ssh_access(_ctx(mode="password", password="secret"), allow_password_bootstrap=True) is True
    assert calls == ["install", "verify"]


def test_run_host_command_uses_public_ensure_host_access(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "tools.lib.host_access.ensure_host_ssh_access",
        lambda ctx, allow_password_bootstrap=False: captured.setdefault("ensure", allow_password_bootstrap),
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("tools.lib.host_access.subprocess.run", fake_run)

    result = run_host_command(_ctx(), "echo ok")

    assert captured["ensure"] is False
    assert "ssh" in captured["command"][0]
    assert result.returncode == 0
