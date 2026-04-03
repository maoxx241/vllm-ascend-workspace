import sys
from pathlib import Path
from types import SimpleNamespace
import importlib

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import vaws
from tools.lib.config import RepoPaths
from tools.lib.machine import add_machine, remove_machine, verify_machine
from tools.lib.machine_registry import list_server_records
from tools.lib.overlay import ensure_overlay_layout


def test_machine_add_records_inventory_without_bootstrapping(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)

    result = add_machine(
        paths,
        "lab-a",
        "10.0.0.8",
        ssh_auth_ref="default",
    )

    assert result == 0
    assert list_server_records(paths)["lab-a"]["host"] == "10.0.0.8"


def test_machine_remove_clears_inventory_only(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    paths.local_servers_file.write_text(
        "version: 1\nservers:\n  lab-a:\n    host: 10.0.0.8\n    port: 22\n    login_user: root\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("tools.lib.machine.cleanup_server_runtime", lambda *_args, **_kwargs: None)

    result = remove_machine(paths, "lab-a")

    assert result == 0
    assert "lab-a" not in list_server_records(paths)
    assert not (vaws_repo / ".workspace.local" / "state.json").exists()


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
    monkeypatch.setattr("tools.lib.machine.probe_runtime_for_server", lambda *_args, **_kwargs: {"status": "ready"})

    assert verify_machine(paths, "lab-a") == 0

    inventory = yaml.safe_load(paths.local_servers_file.read_text(encoding="utf-8"))
    assert inventory["servers"]["lab-a"]["status"] == "ready"
    assert not (vaws_repo / ".workspace.local" / "state.json").exists()


def test_machine_verify_does_not_bootstrap_runtime(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    paths.local_servers_file.write_text(
        "version: 1\nservers:\n  lab-a:\n    host: 10.0.0.8\n    port: 22\n    login_user: root\n    ssh_auth_ref: default\n    runtime:\n      image_ref: image\n      container_name: lab-a\n      ssh_port: 41022\n      workspace_root: /vllm-workspace\n",
        encoding="utf-8",
    )
    called: list[str] = []
    monkeypatch.setattr("tools.lib.machine.probe_runtime_for_server", lambda *_args, **_kwargs: {"status": "needs_repair"})

    assert verify_machine(paths, "lab-a") == 1
    assert called == []


def test_machine_cli_dispatches_to_compat_adapter(monkeypatch, vaws_repo):
    called = {}
    monkeypatch.chdir(vaws_repo)
    monkeypatch.setattr(
        vaws,
        "verify_machine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy machine path called")),
        raising=False,
    )

    def fake_run(paths, args):
        called["root"] = str(paths.root)
        called["machine_command"] = args.machine_command
        called["server_name"] = args.server_name
        return 0

    monkeypatch.setattr(vaws, "vaws_machine", SimpleNamespace(run=fake_run), raising=False)

    assert vaws.main(["machine", "verify", "lab-a"]) == 0
    assert called == {
        "root": str(vaws_repo),
        "machine_command": "verify",
        "server_name": "lab-a",
    }


def test_machine_compat_verify_uses_probe_only(monkeypatch, vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    paths.local_servers_file.write_text(
        "version: 1\nservers:\n  lab-a:\n    host: 10.0.0.8\n    port: 22\n    login_user: root\n    ssh_auth_ref: default\n    runtime:\n      image_ref: image\n      container_name: lab-a\n      ssh_port: 41022\n      workspace_root: /vllm-workspace\n",
        encoding="utf-8",
    )

    vaws_machine = importlib.import_module("tools.lib.vaws_machine")
    called: list[str] = []
    monkeypatch.setattr(
        vaws_machine,
        "probe_runtime_for_server",
        lambda *_args, **_kwargs: {"status": "needs_repair"},
    )

    args = vaws.build_parser().parse_args(["machine", "verify", "lab-a"])
    assert vaws_machine.run(paths, args) == 1
    assert called == []
