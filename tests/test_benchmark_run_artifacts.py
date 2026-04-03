from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.benchmark_run_artifacts import (
    benchmark_run_artifact_path,
    list_benchmark_run_artifacts,
    load_benchmark_run_artifact,
    write_benchmark_run_artifact,
)
from tools.lib.config import RepoPaths


def test_write_benchmark_run_artifact_creates_local_json_record(tmp_path):
    paths = RepoPaths(root=tmp_path)

    artifact_path = write_benchmark_run_artifact(
        paths,
        "run-001",
        {"finished_at": 1, "summary": "ok"},
    )

    assert artifact_path == benchmark_run_artifact_path(paths, "run-001")
    assert artifact_path.exists()
    assert load_benchmark_run_artifact(paths, "run-001")["summary"] == "ok"


def test_list_benchmark_run_artifacts_orders_by_finished_at_desc(tmp_path):
    paths = RepoPaths(root=tmp_path)
    write_benchmark_run_artifact(paths, "run-001", {"finished_at": 1, "summary": "old"})
    write_benchmark_run_artifact(paths, "run-002", {"finished_at": 2, "summary": "new"})

    payloads = list_benchmark_run_artifacts(paths)

    assert [payload["run_id"] for payload in payloads] == ["run-002", "run-001"]
