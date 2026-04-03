import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.remote_types import CredentialGroup, HostSpec, RuntimeSpec, TargetContext
from tools.lib.runtime_bootstrap import (
    RUNTIME_PORT_MAX,
    RUNTIME_PORT_MIN,
    allocate_runtime_ssh_port,
)
from tools.lib.runtime_container import container_requires_rebuild


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
            ssh_port=42321,
            workspace_root="/vllm-workspace",
            bootstrap_mode="host-then-container",
            host_workspace_path="/root/.vaws/targets/lab-a/workspace",
            docker_run_args=[],
        ),
    )


def test_allocate_runtime_ssh_port_picks_free_high_port(monkeypatch):
    sequence = iter([40001, 40002, 42321])
    monkeypatch.setattr("tools.lib.runtime_bootstrap._list_listening_ports", lambda: {40001, 40002})
    monkeypatch.setattr("tools.lib.runtime_bootstrap.random.randint", lambda _lo, _hi: next(sequence))

    port = allocate_runtime_ssh_port()

    assert RUNTIME_PORT_MIN <= port <= RUNTIME_PORT_MAX
    assert port == 42321


def test_container_requires_rebuild_when_network_mode_drifted(monkeypatch):
    ctx = _host_ctx()
    monkeypatch.setattr(
        "tools.lib.runtime_container.inspect_container_contract",
        lambda _ctx: {
            "image": ctx.runtime.image_ref,
            "network_mode": "bridge",
            "mounts": [
                f"{ctx.runtime.host_workspace_path}:{ctx.runtime.workspace_root}/workspace"
            ],
            "sshd_config": (
                f"Port {ctx.runtime.ssh_port}\n"
                "PermitRootLogin prohibit-password\n"
                "PasswordAuthentication no\n"
                "KbdInteractiveAuthentication no\n"
            ),
        },
    )

    assert container_requires_rebuild(ctx) is True
