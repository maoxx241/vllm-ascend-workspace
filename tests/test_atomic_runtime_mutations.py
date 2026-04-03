from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.atomic.machine_bootstrap_host_ssh import bootstrap_host_ssh_access
from tools.atomic.runtime_cleanup_server import cleanup_runtime_server
from tools.atomic.runtime_reconcile_container import reconcile_runtime_container
from tools.lib.config import RepoPaths
from tools.lib.remote_types import RemoteError


def test_bootstrap_host_ssh_access_declares_side_effects(vaws_repo, monkeypatch):
    monkeypatch.setattr(
        "tools.atomic.machine_bootstrap_host_ssh.resolve_server_context",
        lambda _paths, _server_name: object(),
    )
    monkeypatch.setattr(
        "tools.atomic.machine_bootstrap_host_ssh.ensure_host_ssh_access",
        lambda _ctx, allow_password_bootstrap=False: True,
    )

    result = bootstrap_host_ssh_access(RepoPaths(root=vaws_repo), "lab-a")

    assert result["status"] == "ready"
    assert result["side_effects"] == ["host authorized_keys updated", "ssh-key login verified"]


def test_reconcile_runtime_container_reports_reused_container(vaws_repo, monkeypatch):
    monkeypatch.setattr(
        "tools.atomic.runtime_reconcile_container.resolve_server_context",
        lambda _paths, _server_name: object(),
    )
    monkeypatch.setattr(
        "tools.atomic.runtime_reconcile_container.ensure_host_container",
        lambda _ctx: True,
    )

    result = reconcile_runtime_container(RepoPaths(root=vaws_repo), "lab-a")

    assert result["status"] == "ready"
    assert result["side_effects"] == ["runtime container reconciled"]
    assert result["idempotent"] is True


def test_cleanup_runtime_server_returns_structured_failure(vaws_repo, monkeypatch):
    monkeypatch.setattr(
        "tools.atomic.runtime_cleanup_server.resolve_server_context",
        lambda _paths, _server_name: object(),
    )

    def fail(_ctx):
        raise RemoteError("container busy")

    monkeypatch.setattr("tools.atomic.runtime_cleanup_server.destroy_host_runtime", fail)

    result = cleanup_runtime_server(RepoPaths(root=vaws_repo), "lab-a")

    assert result["status"] == "cleanup_failed"
    assert result["reason"] == "container busy"
    assert result["side_effects"] == []
    assert result["retryable"] is True


def test_machine_runtime_family_lists_mutation_capable_tools():
    manifest = (ROOT / ".agents/discovery/families/machine-runtime.yaml").read_text(encoding="utf-8")
    for tool_id in (
        "machine.bootstrap_host_ssh",
        "machine.sync_workspace_mirror",
        "runtime.reconcile_container",
        "runtime.bootstrap_container_transport",
        "runtime.cleanup_server",
    ):
        assert tool_id in manifest
