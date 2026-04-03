import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.atomic.machine_probe_host_ssh import probe_host_ssh
from tools.atomic.runtime_probe_container_transport import probe_container_transport
from tools.lib.config import RepoPaths


def test_probe_host_ssh_returns_ready_without_side_effects(vaws_repo, monkeypatch):
    monkeypatch.setattr(
        "tools.atomic.machine_probe_host_ssh.resolve_server_context",
        lambda paths, server_name: object(),
    )
    monkeypatch.setattr(
        "tools.atomic.machine_probe_host_ssh.verify_host_ssh_key_login",
        lambda ctx: None,
    )

    result = probe_host_ssh(RepoPaths(root=vaws_repo), "lab-a")

    assert result["status"] == "ready"
    assert result["observations"] == ["host ssh key login succeeded for lab-a"]
    assert "side_effects" not in result


def test_probe_container_transport_reports_gap_without_bootstrap(vaws_repo, monkeypatch):
    monkeypatch.setattr(
        "tools.atomic.runtime_probe_container_transport.resolve_server_context",
        lambda paths, server_name: object(),
    )
    monkeypatch.setattr(
        "tools.atomic.runtime_probe_container_transport.probe_container_ssh_transport",
        lambda ctx: ("needs_repair", "container ssh probe failed"),
    )
    monkeypatch.setattr(
        "tools.atomic.runtime_probe_container_transport.probe_docker_exec_transport",
        lambda ctx: ("needs_repair", "docker exec probe failed"),
    )

    result = probe_container_transport(RepoPaths(root=vaws_repo), "lab-a")

    assert result["status"] == "needs_repair"
    assert result["reason"] == "no runtime transport is ready"
    assert result["next_probes"] == ["runtime.bootstrap_container_transport"]
    assert "side_effects" not in result
