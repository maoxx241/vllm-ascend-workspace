from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import RepoPaths


def benchmark_run_artifact_path(paths: RepoPaths, run_id: str) -> Path:
    return paths.local_benchmark_runs_dir / f"{run_id}.json"


def write_benchmark_run_artifact(paths: RepoPaths, run_id: str, payload: dict[str, object]) -> Path:
    paths.local_benchmark_runs_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = benchmark_run_artifact_path(paths, run_id)
    record = dict(payload)
    record.setdefault("run_id", run_id)
    record["artifact_path"] = str(artifact_path)
    artifact_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return artifact_path


def load_benchmark_run_artifact(paths: RepoPaths, run_id: str) -> dict[str, object]:
    artifact_path = benchmark_run_artifact_path(paths, run_id)
    if not artifact_path.is_file():
        raise RuntimeError(f"unknown benchmark run: {run_id}")
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid benchmark run artifact: {run_id}")
    payload.setdefault("run_id", run_id)
    payload.setdefault("artifact_path", str(artifact_path))
    return payload


def list_benchmark_run_artifacts(paths: RepoPaths) -> list[dict[str, object]]:
    if not paths.local_benchmark_runs_dir.is_dir():
        return []

    payloads: list[dict[str, object]] = []
    for artifact_path in sorted(paths.local_benchmark_runs_dir.glob("*.json")):
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        run_id = str(payload.get("run_id") or artifact_path.stem)
        payload.setdefault("run_id", run_id)
        payload.setdefault("artifact_path", str(artifact_path))
        payloads.append(payload)

    payloads.sort(key=lambda payload: int(payload.get("finished_at", 0)), reverse=True)
    return payloads
