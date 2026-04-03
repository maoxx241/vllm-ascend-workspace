from __future__ import annotations

from pathlib import Path

from tools.lib.remote_types import CredentialGroup, HostSpec, RuntimeSpec, TargetContext


def build_simulation_target_context(
    *,
    name: str = "sim-box",
    simulation_root: str | Path = "/tmp/vaws-sim",
    workspace_root: str = "/vllm-workspace",
) -> TargetContext:
    return TargetContext(
        name=name,
        host=HostSpec(
            name=name,
            host="sim.local",
            port=22,
            login_user="root",
            auth_group="sim-auth",
            ssh_auth_ref="sim-auth",
        ),
        credential=CredentialGroup(
            mode="local-simulation",
            username="root",
            simulation_root=Path(simulation_root),
        ),
        runtime=RuntimeSpec(
            image_ref="image",
            container_name="container",
            ssh_port=41001,
            workspace_root=workspace_root,
            bootstrap_mode="host-then-container",
            host_workspace_path=f"/root/.vaws/targets/{name}/workspace",
            docker_run_args=[],
        ),
    )
