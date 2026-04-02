import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.config import RepoPaths
from tools.lib.machine import remove_machine, verify_machine
from tools.lib.overlay import ensure_overlay_layout
from tools.lib.runtime import read_state, write_state


def test_machine_remove_clears_inventory_and_current_selection(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    paths.local_servers_file.write_text(
        "version: 1\nservers:\n  lab-a:\n    host: 10.0.0.8\n    port: 22\n    login_user: root\n",
        encoding="utf-8",
    )
    write_state(
        paths,
        {
            "schema_version": 2,
            "servers": {"lab-a": {"host_access": {"status": "ready"}}},
            "current_server": "lab-a",
            "current_session": "feature-a",
        },
    )
    monkeypatch.setattr("tools.lib.machine.cleanup_server_runtime", lambda *_args, **_kwargs: None)

    result = remove_machine(paths, "lab-a")
    state = read_state(paths)

    assert result == 0
    assert state.get("current_server") is None
    assert state.get("current_session") is None
    assert "lab-a" not in state.get("servers", {})


def test_machine_remove_reports_partial_cleanup_when_remote_cleanup_fails(vaws_repo, monkeypatch, capsys):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    paths.local_servers_file.write_text(
        "version: 1\nservers:\n  lab-a:\n    host: 10.0.0.8\n    port: 22\n    login_user: root\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "tools.lib.machine.cleanup_server_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert remove_machine(paths, "lab-a") == 1
    assert "partial" in capsys.readouterr().out.lower()


def test_machine_verify_sets_current_server_and_server_capabilities(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    paths.local_servers_file.write_text(
        "version: 1\nservers:\n  lab-a:\n    host: 10.0.0.8\n    port: 22\n    login_user: root\n    ssh_auth_ref: default\n    runtime:\n      image_ref: image\n      container_name: lab-a\n      ssh_port: 41022\n      workspace_root: /vllm-workspace\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("tools.lib.machine.resolve_server_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("tools.lib.machine.ensure_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("tools.lib.machine.verify_runtime", lambda *_args, **_kwargs: {"status": "ready"})

    assert verify_machine(paths, "lab-a") == 0

    state = read_state(paths)
    assert state["current_server"] == "lab-a"
    assert state["servers"]["lab-a"]["container_access"]["status"] == "ready"
