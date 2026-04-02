import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.capability_state import read_capability_state, write_capability_leaf
from tools.lib.config import RepoPaths
from tools.lib.remote import reconcile_peer_mesh


def seed_ready_peer_mesh_state(paths: RepoPaths, server_names: list[str]) -> None:
    for index, server_name in enumerate(server_names, start=1):
        write_capability_leaf(
            paths,
            ("servers", server_name, "container_access"),
            {
                "status": "ready",
                "detail": "container ssh ok",
                "observed_at": f"2026-04-01T12:00:0{index}Z",
                "evidence_source": "machine-management",
            },
        )


def test_peer_mesh_installs_peer_keys_when_two_ready_servers_are_reachable(monkeypatch, vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    seed_ready_peer_mesh_state(paths, ["lab-a", "lab-b"])
    calls = []

    monkeypatch.setattr("tools.lib.remote._probe_peer_connectivity", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "tools.lib.remote._install_peer_authorized_key",
        lambda *args, **kwargs: calls.append("authorized_keys"),
    )
    monkeypatch.setattr(
        "tools.lib.remote._warm_peer_known_hosts",
        lambda *args, **kwargs: calls.append("known_hosts"),
    )

    reconcile_peer_mesh(paths, "lab-a")

    assert calls.count("authorized_keys") == 2
    assert calls.count("known_hosts") == 2
    state = read_capability_state(paths)
    assert state["servers"]["lab-a"]["peer_mesh"]["status"] == "ready"


def test_peer_mesh_marks_degraded_optional_when_peer_is_unreachable(monkeypatch, vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    seed_ready_peer_mesh_state(paths, ["lab-a", "lab-b"])
    monkeypatch.setattr("tools.lib.remote._probe_peer_connectivity", lambda *args, **kwargs: False)

    reconcile_peer_mesh(paths, "lab-a")

    state = read_capability_state(paths)
    assert state["servers"]["lab-a"]["peer_mesh"]["status"] == "degraded_optional"
