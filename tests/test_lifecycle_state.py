import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.config import RepoPaths
from tools.lib.lifecycle_state import (
    LEGACY_TARGET_HANDOFF_KIND,
    infer_current_target_kind,
    get_lifecycle_state,
    record_foundation_status,
    record_git_profile_status,
    record_requested_mode,
    record_runtime_handoff,
)
from tools.lib.overlay import ensure_overlay_layout
from tools.lib.runtime import read_state, write_state
from tools.lib.remote import CredentialGroup, HostSpec, RuntimeSpec, TargetContext


def test_record_requested_mode_and_skill_statuses(tmp_path):
    paths = RepoPaths(root=tmp_path)
    ensure_overlay_layout(paths)
    write_state(paths, {})

    assert read_state(paths)["schema_version"] == 1

    record_requested_mode(paths, "foundation")
    record_foundation_status(paths, "ready")
    record_git_profile_status(paths, "pending")

    lifecycle = get_lifecycle_state(paths)
    state = read_state(paths)

    assert lifecycle["requested_mode"] == "foundation"
    assert state["lifecycle"]["requested_mode"] == "foundation"
    assert state["lifecycle"]["foundation"]["status"] == "ready"
    assert state["lifecycle"]["git_profile"]["status"] == "pending"


def test_record_runtime_handoff_sets_current_target_and_runtime(tmp_path):
    paths = RepoPaths(root=tmp_path)
    ensure_overlay_layout(paths)
    write_state(paths, {"lifecycle": {}})

    runtime = {
        "workspace_root": "/vllm-workspace",
        "ssh_port": 63269,
        "transport": "bootstrap",
    }

    record_runtime_handoff(
        paths,
        current_target="single-default",
        handoff_kind=LEGACY_TARGET_HANDOFF_KIND,
        runtime=runtime,
    )

    state = read_state(paths)

    assert state["current_target"] == "single-default"
    assert state["current_target_kind"] == LEGACY_TARGET_HANDOFF_KIND
    assert state["runtime"] == runtime


def test_get_lifecycle_state_returns_empty_without_mutating_state_file(tmp_path):
    paths = RepoPaths(root=tmp_path)
    ensure_overlay_layout(paths)
    write_state(paths, {"schema_version": 1})

    before = (paths.local_state_file).read_text(encoding="utf-8")

    lifecycle = get_lifecycle_state(paths)

    after = (paths.local_state_file).read_text(encoding="utf-8")

    assert lifecycle == {}
    assert after == before


def test_infer_current_target_kind_prefers_runtime_match_over_colliding_name(
    tmp_path,
    monkeypatch,
):
    paths = RepoPaths(root=tmp_path)
    ensure_overlay_layout(paths)
    write_state(
        paths,
        {
            "schema_version": 1,
            "current_target": "shared-name",
            "runtime": {
                "container_name": "legacy-owner",
                "ssh_port": 63269,
                "workspace_root": "/vllm-workspace",
                "bootstrap_mode": "host-then-container",
                "host_workspace_path": "/root/.vaws/targets/shared-name/workspace",
                "host_name": "host-a",
            },
        },
    )

    legacy_ctx = TargetContext(
        name="shared-name",
        host=HostSpec(
            name="host-a",
            host="127.0.0.1",
            port=22,
            login_user="root",
            auth_group="shared-lab-a",
        ),
        credential=CredentialGroup(mode="local-simulation", username="root"),
        runtime=RuntimeSpec(
            image_ref="registry.example.com/ascend/vllm-ascend:test",
            container_name="legacy-owner",
            ssh_port=63269,
            workspace_root="/vllm-workspace",
            bootstrap_mode="host-then-container",
            host_workspace_path="/root/.vaws/targets/shared-name/workspace",
            docker_run_args=[],
        ),
    )
    managed_ctx = TargetContext(
        name="shared-name",
        host=HostSpec(
            name="shared-name",
            host="127.0.0.1",
            port=22,
            login_user="root",
            auth_group="shared-lab-a",
        ),
        credential=CredentialGroup(mode="local-simulation", username="root"),
        runtime=RuntimeSpec(
            image_ref="registry.example.com/ascend/vllm-ascend:test",
            container_name="server-owner",
            ssh_port=63270,
            workspace_root="/vllm-workspace",
            bootstrap_mode="host-then-container",
            host_workspace_path="/root/.vaws/targets/shared-name/workspace",
            docker_run_args=[],
        ),
    )

    monkeypatch.setattr("tools.lib.remote.resolve_server_context", lambda paths, name: managed_ctx)
    monkeypatch.setattr("tools.lib.remote.resolve_target_context", lambda paths, name: legacy_ctx)

    kind = infer_current_target_kind(paths, "shared-name", read_state(paths)["runtime"])

    assert kind == LEGACY_TARGET_HANDOFF_KIND
