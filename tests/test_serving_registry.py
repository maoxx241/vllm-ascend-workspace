import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.capability_state import (  # type: ignore[attr-defined]
    read_capability_state,
    record_benchmark_run,
    remove_service_session,
    upsert_service_session,
)
from tools.lib.config import RepoPaths


def test_default_capability_state_includes_services_and_benchmark_runs(vaws_repo):
    paths = RepoPaths(root=vaws_repo)

    state = read_capability_state(paths)

    assert state == {
        "schema_version": 3,
        "servers": {},
        "services": {},
        "benchmark_runs": {},
    }


def test_read_capability_state_migrates_v2_file_to_v3_shape(vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    paths.local_state_file.parent.mkdir(parents=True, exist_ok=True)
    paths.local_state_file.write_text(
        (
            '{"schema_version": 2, "servers": {"box-a": {}}, '
            '"code_parity": {"box-a": {"status": "ready"}}, '
            '"runtime_environment": {"box-a": {"workspace_commit": "abc123"}}}\n'
        ),
        encoding="utf-8",
    )

    state = read_capability_state(paths)

    assert state == {
        "schema_version": 3,
        "servers": {"box-a": {}},
        "services": {},
        "benchmark_runs": {},
        "code_parity": {"box-a": {"status": "ready"}},
        "runtime_environment": {"box-a": {"workspace_commit": "abc123"}},
    }


def test_upsert_service_session_persists_workspace_global_registry(vaws_repo):
    paths = RepoPaths(root=vaws_repo)

    upsert_service_session(
        paths,
        {
            "service_id": "svc-1",
            "server_name": "box-a",
            "topology": "single-node-replica",
            "model_profile": "qwen3_5",
            "lifecycle": "explicit-serving",
        },
    )

    state = read_capability_state(paths)
    assert state["services"]["svc-1"]["server_name"] == "box-a"
    assert state["services"]["svc-1"]["lifecycle"] == "explicit-serving"


def test_record_benchmark_run_uses_run_id_not_preset_name(vaws_repo):
    paths = RepoPaths(root=vaws_repo)

    record_benchmark_run(
        paths,
        "run-001",
        {
            "preset_name": "qwen3_5_35b_tp4_perf",
            "service_id": "svc-1",
            "server_name": "box-a",
        },
    )
    record_benchmark_run(
        paths,
        "run-002",
        {
            "preset_name": "qwen3_5_35b_tp4_perf",
            "service_id": "svc-2",
            "server_name": "box-b",
        },
    )

    state = read_capability_state(paths)
    assert set(state["benchmark_runs"]) == {"run-001", "run-002"}


def test_remove_service_session_deletes_temporary_service(vaws_repo):
    paths = RepoPaths(root=vaws_repo)

    upsert_service_session(
        paths,
        {
            "service_id": "svc-temp",
            "server_name": "box-a",
            "lifecycle": "benchmark-temporary",
        },
    )

    remove_service_session(paths, "svc-temp")

    state = read_capability_state(paths)
    assert "svc-temp" not in state["services"]
