import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib import session as session_lib
from tools.lib.config import RepoPaths
from tools.lib.overlay import ensure_overlay_layout
from tools.lib.remote import CredentialGroup, HostSpec, RuntimeSpec, TargetContext
from tools.lib.runtime import read_state, write_state


def _ready_state(server_name: str = "lab-a") -> dict:
    return {
        "schema_version": 2,
        "servers": {
            server_name: {
                "host_access": {
                    "status": "ready",
                    "mode": "ssh-key",
                    "detail": "host ssh ready",
                },
                "container_access": {
                    "status": "ready",
                    "mode": "ssh-key",
                    "detail": "container ssh ready",
                },
            }
        },
        "current_server": server_name,
    }


def _server_context(server_name: str = "lab-a") -> TargetContext:
    return TargetContext(
        name=server_name,
        host=HostSpec(
            name=server_name,
            host="10.0.0.12",
            port=22,
            login_user="root",
            auth_group="shared-lab-a",
            ssh_auth_ref="shared-lab-a",
        ),
        credential=CredentialGroup(mode="ssh-key", username="root"),
        runtime=RuntimeSpec(
            image_ref="quay.nju.edu.cn/ascend/vllm-ascend:latest",
            container_name="vaws-workspace",
            ssh_port=41001,
            workspace_root="/vllm-workspace",
            bootstrap_mode="host-then-container",
            host_workspace_path="/root/.vaws/targets/lab-a/workspace",
            docker_run_args=[],
        ),
    )


def test_session_requires_current_server_not_current_target(vaws_repo, capsys):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    write_state(paths, {"schema_version": 2, "servers": {}})

    assert session_lib.create_session(paths, "feature-a") == 1

    output = capsys.readouterr().out.lower()
    assert "current server" in output
    assert "current target" not in output


def test_session_create_uses_current_server_and_container_ssh(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    write_state(paths, _ready_state())

    calls = {}

    monkeypatch.setattr(session_lib, "resolve_server_context", lambda *_args: _server_context())

    def fake_create_remote_session(_paths, ctx, manifest, transport):
        calls["server_name"] = ctx.name
        calls["manifest_target"] = manifest["target"]
        calls["transport"] = transport

    monkeypatch.setattr(session_lib, "create_remote_session", fake_create_remote_session)

    assert session_lib.create_session(paths, "feature_a") == 0

    manifest = yaml.safe_load(
        (paths.local_sessions_dir / "feature_a" / "manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["target"] == "lab-a"
    assert manifest["runtime"]["venv_path"].startswith("/vllm-workspace/.vaws/sessions/feature_a")
    assert calls == {
        "server_name": "lab-a",
        "manifest_target": "lab-a",
        "transport": "container-ssh",
    }


def test_session_switch_updates_current_session_in_capability_state(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    write_state(paths, _ready_state())

    session_dir = paths.local_sessions_dir / "feature_b"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "manifest.yaml").write_text("name: feature_b\n", encoding="utf-8")

    calls = {}

    monkeypatch.setattr(session_lib, "resolve_server_context", lambda *_args: _server_context())

    def fake_switch_remote_session(ctx, session_name, transport):
        calls["server_name"] = ctx.name
        calls["session_name"] = session_name
        calls["transport"] = transport

    monkeypatch.setattr(session_lib, "switch_remote_session", fake_switch_remote_session)

    assert session_lib.switch_session(paths, "feature_b") == 0

    state = read_state(paths)
    assert state["current_server"] == "lab-a"
    assert state["current_session"] == "feature_b"
    assert calls == {
        "server_name": "lab-a",
        "session_name": "feature_b",
        "transport": "container-ssh",
    }


def test_session_status_reports_current_server_selection(vaws_repo, capsys):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    state = _ready_state()
    state["current_session"] = "feature_c"
    write_state(paths, state)

    assert session_lib.status_session(paths) == 0

    output = capsys.readouterr().out.lower()
    assert "current server: lab-a" in output
    assert "current session: feature_c" in output
    assert "current target" not in output
