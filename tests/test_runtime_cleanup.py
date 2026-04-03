from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.remote_types import CredentialGroup, HostSpec, RuntimeSpec, TargetContext
from tools.lib.runtime_cleanup import destroy_host_runtime


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
            image_ref="image",
            container_name="container",
            ssh_port=63269,
            workspace_root="/vllm-workspace",
            bootstrap_mode="host-then-container",
            host_workspace_path="/root/.vaws/targets/lab-a/workspace",
            docker_run_args=[],
        ),
    )


def test_destroy_host_runtime_removes_container_and_workspace(monkeypatch):
    recorded: dict[str, str] = {}

    def fake_run_host_command(_ctx, script):
        recorded["script"] = script
        return subprocess.CompletedProcess(["ssh"], 0, "", "")

    monkeypatch.setattr("tools.lib.runtime_cleanup.run_host_command", fake_run_host_command)

    destroy_host_runtime(_ctx())

    assert "docker rm -f" in recorded["script"]
    assert "/root/.vaws/targets/lab-a/workspace" in recorded["script"]
