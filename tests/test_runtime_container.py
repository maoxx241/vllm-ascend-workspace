from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.remote_types import CredentialGroup, HostSpec, RuntimeSpec, TargetContext
from tools.lib.runtime_container import ensure_host_container


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
            ssh_port=42321,
            workspace_root="/vllm-workspace",
            bootstrap_mode="host-then-container",
            host_workspace_path="/root/.vaws/targets/lab-a/workspace",
            docker_run_args=[],
        ),
    )


def test_ensure_host_container_reuses_matching_running_container(monkeypatch):
    monkeypatch.setattr(
        "tools.lib.runtime_container.run_host_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["ssh"], 0, "true\n", ""),
    )
    monkeypatch.setattr(
        "tools.lib.runtime_container.container_requires_rebuild",
        lambda ctx: False,
    )

    assert ensure_host_container(_ctx()) is True
