#!/usr/bin/env python3
"""Manage graph-debug cases and compare graph/eager JSONL snapshots."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_run_manifest import (  # noqa: E402
    RunManifestError,
    add_artifact,
    load_manifest,
    new_manifest,
    transition_status,
    write_manifest,
)

CASE_SCHEMA_VERSION = 1
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
STAGES = ("compile", "capture", "replay", "accuracy", "unknown")
BASELINE_RESULTS = ("pass", "fail", "not-run")
SNAPSHOT_KEY_FIELDS = ("step", "layer", "rank", "tag")


class GraphDebugError(ValueError):
    """Raised when a graph-debug case or snapshot is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def emit_progress(phase: str, **details: Any) -> None:
    print(json.dumps({"phase": phase, **details}, ensure_ascii=False), file=sys.stderr)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GraphDebugError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise GraphDebugError(f"{label} must be a JSON object")
    return value


def _case_path(case_dir: Path) -> Path:
    return case_dir / "case.json"


def _manifest_path(case_dir: Path) -> Path:
    return case_dir / "manifest.json"


def _validate_case(case: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if case.get("schema_version") != CASE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {CASE_SCHEMA_VERSION}")
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not SAFE_ID_RE.fullmatch(case_id):
        errors.append("case_id must be a lowercase safe identifier")
    if case.get("stage") not in STAGES:
        errors.append(f"stage must be one of: {', '.join(STAGES)}")
    baselines = case.get("baselines")
    if not isinstance(baselines, Mapping):
        errors.append("baselines must be an object")
    else:
        for mode in ("eager", "graph"):
            if baselines.get(mode) not in BASELINE_RESULTS:
                errors.append(
                    f"baselines.{mode} must be one of: {', '.join(BASELINE_RESULTS)}"
                )
    for field in ("environment", "workspace_snapshot", "model", "topology"):
        if not isinstance(case.get(field), Mapping):
            errors.append(f"{field} must be an object")
    if not isinstance(case.get("reproduction"), str) or not case["reproduction"].strip():
        errors.append("reproduction must be a non-empty string")
    for field in ("experiments", "comparisons"):
        if not isinstance(case.get(field), list):
            errors.append(f"{field} must be an array")
    if case.get("status") not in {"active", "resolved", "inconclusive"}:
        errors.append("status must be active, resolved, or inconclusive")
    if errors:
        raise GraphDebugError("; ".join(errors))


def load_case(case_dir: Path) -> dict[str, Any]:
    path = _case_path(case_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphDebugError(f"cannot read graph-debug case {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GraphDebugError("case root must be an object")
    _validate_case(payload)
    return payload


def init_case(
    case_dir: Path,
    *,
    case_id: str,
    stage: str,
    eager_result: str,
    graph_result: str,
    reproduction: str,
    environment: Mapping[str, Any] | None = None,
    workspace_snapshot: Mapping[str, Any] | None = None,
    model: Mapping[str, Any] | None = None,
    topology: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    if case_dir.exists() and any(case_dir.iterdir()):
        raise GraphDebugError(f"case directory is not empty: {case_dir}")
    timestamp = created_at or utc_now()
    case: dict[str, Any] = {
        "schema_version": CASE_SCHEMA_VERSION,
        "case_id": case_id,
        "stage": stage,
        "baselines": {"eager": eager_result, "graph": graph_result},
        "environment": dict(environment or {}),
        "workspace_snapshot": dict(workspace_snapshot or {}),
        "model": dict(model or {}),
        "topology": dict(topology or {}),
        "reproduction": reproduction,
        "known_facts": [],
        "excluded": [],
        "current_suspects": [],
        "experiments": [],
        "comparisons": [],
        "resolution": None,
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _validate_case(case)
    manifest = new_manifest(
        run_type="debug",
        run_id=case_id,
        workspace_snapshot=workspace_snapshot,
        environment=environment,
        model=model,
        topology=topology,
        command=[],
        created_at=timestamp,
    )
    case_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(_case_path(case_dir), case)
    write_manifest(_manifest_path(case_dir), manifest)
    return case


def _ensure_manifest_running(case_dir: Path, *, updated_at: str | None = None) -> dict[str, Any]:
    manifest = load_manifest(_manifest_path(case_dir))
    if manifest["status"] == "planned":
        manifest = transition_status(manifest, "running", updated_at=updated_at)
        write_manifest(_manifest_path(case_dir), manifest)
    if manifest["status"] != "running":
        raise GraphDebugError(
            f"case manifest is already terminal: {manifest['status']}"
        )
    return manifest


def record_experiment(
    case_dir: Path,
    *,
    variable: str,
    hypothesis: str,
    expected: str,
    observed: str,
    conclusion: str,
    next_step: str,
    updated_at: str | None = None,
) -> dict[str, Any]:
    case = load_case(case_dir)
    if case["status"] != "active":
        raise GraphDebugError(f"cannot record experiment on {case['status']} case")
    values = {
        "variable": variable,
        "hypothesis": hypothesis,
        "expected": expected,
        "observed": observed,
        "conclusion": conclusion,
        "next_step": next_step,
    }
    for field, value in values.items():
        if not value.strip():
            raise GraphDebugError(f"{field} must be non-empty")
    timestamp = updated_at or utc_now()
    experiment = {
        "index": len(case["experiments"]) + 1,
        **values,
        "recorded_at": timestamp,
    }
    case["experiments"].append(experiment)
    case["updated_at"] = timestamp
    _validate_case(case)
    _ensure_manifest_running(case_dir, updated_at=timestamp)
    _atomic_write_json(_case_path(case_dir), case)
    return experiment


def _snapshot_key(record: Mapping[str, Any], *, path: Path, line_number: int) -> tuple:
    values: list[Any] = []
    for field in SNAPSHOT_KEY_FIELDS:
        if field not in record:
            raise GraphDebugError(f"{path}:{line_number}: missing snapshot key {field}")
        value = record[field]
        if field == "tag":
            if not isinstance(value, str) or not value:
                raise GraphDebugError(f"{path}:{line_number}: tag must be non-empty")
        elif not isinstance(value, int) or isinstance(value, bool):
            raise GraphDebugError(f"{path}:{line_number}: {field} must be an integer")
        values.append(value)
    return tuple(values)


def load_snapshots(path: Path) -> dict[tuple, dict[str, Any]]:
    records: dict[tuple, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GraphDebugError(f"cannot read snapshot file {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GraphDebugError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise GraphDebugError(f"{path}:{line_number}: snapshot must be an object")
        key = _snapshot_key(record, path=path, line_number=line_number)
        if key in records:
            raise GraphDebugError(f"{path}:{line_number}: duplicate snapshot key {key}")
        records[key] = record
    if not records:
        raise GraphDebugError(f"snapshot file has no records: {path}")
    return records


def _flatten_numbers(value: Any, *, label: str) -> list[float]:
    if isinstance(value, bool):
        raise GraphDebugError(f"{label} contains a boolean instead of a number")
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, list):
        flattened: list[float] = []
        for index, item in enumerate(value):
            flattened.extend(_flatten_numbers(item, label=f"{label}[{index}]"))
        return flattened
    raise GraphDebugError(f"{label} must contain only numbers or nested arrays")


def _numbers_close(left: float, right: float, *, atol: float, rtol: float) -> bool:
    if math.isnan(left) or math.isnan(right):
        return math.isnan(left) and math.isnan(right)
    if math.isinf(left) or math.isinf(right):
        return left == right
    return math.isclose(left, right, abs_tol=atol, rel_tol=rtol)


def _compare_record(
    eager: Mapping[str, Any],
    graph: Mapping[str, Any],
    *,
    atol: float,
    rtol: float,
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    eager_stats = eager.get("stats", {})
    graph_stats = graph.get("stats", {})
    if not isinstance(eager_stats, Mapping) or not isinstance(graph_stats, Mapping):
        raise GraphDebugError("snapshot stats must be objects")
    for name in sorted(set(eager_stats) | set(graph_stats)):
        if name not in eager_stats or name not in graph_stats:
            differences.append({"field": f"stats.{name}", "reason": "missing"})
            continue
        left = _flatten_numbers(eager_stats[name], label=f"eager.stats.{name}")
        right = _flatten_numbers(graph_stats[name], label=f"graph.stats.{name}")
        if len(left) != len(right):
            differences.append(
                {
                    "field": f"stats.{name}",
                    "reason": "length-mismatch",
                    "eager_length": len(left),
                    "graph_length": len(right),
                }
            )
            continue
        mismatches = [
            index
            for index, (left_value, right_value) in enumerate(zip(left, right))
            if not _numbers_close(left_value, right_value, atol=atol, rtol=rtol)
        ]
        if mismatches:
            index = mismatches[0]
            differences.append(
                {
                    "field": f"stats.{name}",
                    "reason": "numeric-divergence",
                    "first_index": index,
                    "eager": left[index],
                    "graph": right[index],
                    "mismatch_count": len(mismatches),
                }
            )

    if "sample" in eager or "sample" in graph:
        if "sample" not in eager or "sample" not in graph:
            differences.append({"field": "sample", "reason": "missing"})
        else:
            left = _flatten_numbers(eager["sample"], label="eager.sample")
            right = _flatten_numbers(graph["sample"], label="graph.sample")
            if len(left) != len(right):
                differences.append(
                    {
                        "field": "sample",
                        "reason": "length-mismatch",
                        "eager_length": len(left),
                        "graph_length": len(right),
                    }
                )
            else:
                mismatches = [
                    index
                    for index, (left_value, right_value) in enumerate(zip(left, right))
                    if not _numbers_close(left_value, right_value, atol=atol, rtol=rtol)
                ]
                if mismatches:
                    index = mismatches[0]
                    differences.append(
                        {
                            "field": "sample",
                            "reason": "numeric-divergence",
                            "first_index": index,
                            "eager": left[index],
                            "graph": right[index],
                            "mismatch_count": len(mismatches),
                        }
                    )
    return differences


def compare_snapshots(
    eager_path: Path,
    graph_path: Path,
    *,
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    if atol < 0 or rtol < 0:
        raise GraphDebugError("atol and rtol must be non-negative")
    eager = load_snapshots(eager_path)
    graph = load_snapshots(graph_path)
    eager_keys = set(eager)
    graph_keys = set(graph)
    missing_in_graph = sorted(eager_keys - graph_keys)
    missing_in_eager = sorted(graph_keys - eager_keys)
    divergences: list[dict[str, Any]] = []
    for key in sorted(eager_keys & graph_keys):
        differences = _compare_record(eager[key], graph[key], atol=atol, rtol=rtol)
        if differences:
            divergences.append(
                {
                    "key": dict(zip(SNAPSHOT_KEY_FIELDS, key)),
                    "differences": differences,
                }
            )
    for key in missing_in_graph:
        divergences.append(
            {
                "key": dict(zip(SNAPSHOT_KEY_FIELDS, key)),
                "differences": [{"field": "record", "reason": "missing-in-graph"}],
            }
        )
    for key in missing_in_eager:
        divergences.append(
            {
                "key": dict(zip(SNAPSHOT_KEY_FIELDS, key)),
                "differences": [{"field": "record", "reason": "missing-in-eager"}],
            }
        )
    divergences.sort(
        key=lambda row: tuple(row["key"][field] for field in SNAPSHOT_KEY_FIELDS)
    )
    return {
        "schema_version": 1,
        "status": "exact-match" if not divergences else "diverged",
        "eager_snapshot": str(eager_path.resolve()),
        "graph_snapshot": str(graph_path.resolve()),
        "atol": atol,
        "rtol": rtol,
        "eager_count": len(eager),
        "graph_count": len(graph),
        "aligned_count": len(eager_keys & graph_keys),
        "divergence_count": len(divergences),
        "first_divergence": divergences[0] if divergences else None,
        "divergences": divergences,
    }


def compare_case(
    case_dir: Path,
    *,
    eager_path: Path,
    graph_path: Path,
    atol: float,
    rtol: float,
    updated_at: str | None = None,
) -> tuple[dict[str, Any], Path]:
    case = load_case(case_dir)
    if case["status"] != "active":
        raise GraphDebugError(f"cannot compare snapshots on {case['status']} case")
    emit_progress("load-snapshots", eager=str(eager_path), graph=str(graph_path))
    comparison = compare_snapshots(eager_path, graph_path, atol=atol, rtol=rtol)
    index = len(case["comparisons"]) + 1
    output = case_dir / "comparisons" / f"comparison-{index:03d}.json"
    _atomic_write_json(output, comparison)
    timestamp = updated_at or utc_now()
    case["comparisons"].append(
        {
            "index": index,
            "path": output.relative_to(case_dir).as_posix(),
            "status": comparison["status"],
            "first_divergence": comparison["first_divergence"],
            "recorded_at": timestamp,
        }
    )
    case["updated_at"] = timestamp
    _atomic_write_json(_case_path(case_dir), case)

    manifest = _ensure_manifest_running(case_dir, updated_at=timestamp)
    manifest = add_artifact(
        manifest,
        name=f"comparison-{index:03d}",
        kind="graph-eager-comparison",
        uri=output.relative_to(case_dir).as_posix(),
        updated_at=timestamp,
    )
    write_manifest(_manifest_path(case_dir), manifest)
    return comparison, output


def finalize_case(
    case_dir: Path,
    *,
    root_cause: str,
    fix: str,
    minimal_result: str,
    original_result: str,
    cleanup_status: str,
    updated_at: str | None = None,
) -> dict[str, Any]:
    case = load_case(case_dir)
    if case["status"] != "active":
        raise GraphDebugError(f"case is already {case['status']}")
    if minimal_result not in {"pass", "fail"} or original_result not in {"pass", "fail"}:
        raise GraphDebugError("minimal-result and original-result must be pass or fail")
    if cleanup_status not in {"removed", "disabled", "pending"}:
        raise GraphDebugError("cleanup-status must be removed, disabled, or pending")
    if not root_cause.strip() or not fix.strip():
        raise GraphDebugError("root-cause and fix must be non-empty")
    resolved = (
        minimal_result == "pass"
        and original_result == "pass"
        and cleanup_status in {"removed", "disabled"}
    )
    timestamp = updated_at or utc_now()
    case["resolution"] = {
        "root_cause": root_cause,
        "fix": fix,
        "minimal_reproduction": minimal_result,
        "original_reproduction": original_result,
        "debug_instrumentation": cleanup_status,
        "resolved_at": timestamp,
    }
    case["status"] = "resolved" if resolved else "inconclusive"
    case["updated_at"] = timestamp
    _validate_case(case)
    _atomic_write_json(_case_path(case_dir), case)

    manifest = _ensure_manifest_running(case_dir, updated_at=timestamp)
    manifest = add_artifact(
        manifest,
        name="graph-debug-case",
        kind="debug-record",
        uri="case.json",
        updated_at=timestamp,
    )
    manifest = transition_status(
        manifest, "passed" if resolved else "inconclusive", updated_at=timestamp
    )
    write_manifest(_manifest_path(case_dir), manifest)
    return case


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    init = subparsers.add_parser("init", help="initialize a graph-debug case")
    init.add_argument("--case-dir", required=True, type=Path)
    init.add_argument("--case-id", required=True)
    init.add_argument("--stage", required=True, choices=STAGES)
    init.add_argument("--eager-result", required=True, choices=BASELINE_RESULTS)
    init.add_argument("--graph-result", required=True, choices=BASELINE_RESULTS)
    init.add_argument("--reproduction", required=True)
    init.add_argument("--environment", default="{}")
    init.add_argument("--workspace-snapshot", default="{}")
    init.add_argument("--model", default="{}")
    init.add_argument("--topology", default="{}")

    record = subparsers.add_parser("record", help="append one controlled experiment")
    record.add_argument("--case-dir", required=True, type=Path)
    record.add_argument("--variable", required=True)
    record.add_argument("--hypothesis", required=True)
    record.add_argument("--expected", required=True)
    record.add_argument("--observed", required=True)
    record.add_argument("--conclusion", required=True)
    record.add_argument("--next-step", required=True)

    compare = subparsers.add_parser("compare", help="compare eager and graph JSONL snapshots")
    compare.add_argument("--case-dir", required=True, type=Path)
    compare.add_argument("--eager", required=True, type=Path)
    compare.add_argument("--graph", required=True, type=Path)
    compare.add_argument("--atol", type=float, default=0.0)
    compare.add_argument("--rtol", type=float, default=0.0)

    finalize = subparsers.add_parser("finalize", help="finalize validation and cleanup")
    finalize.add_argument("--case-dir", required=True, type=Path)
    finalize.add_argument("--root-cause", required=True)
    finalize.add_argument("--fix", required=True)
    finalize.add_argument("--minimal-result", required=True, choices=("pass", "fail"))
    finalize.add_argument("--original-result", required=True, choices=("pass", "fail"))
    finalize.add_argument(
        "--cleanup-status", required=True, choices=("removed", "disabled", "pending")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "init":
            emit_progress("initialize", case_dir=str(args.case_dir))
            case = init_case(
                args.case_dir,
                case_id=args.case_id,
                stage=args.stage,
                eager_result=args.eager_result,
                graph_result=args.graph_result,
                reproduction=args.reproduction,
                environment=_json_object(args.environment, "environment"),
                workspace_snapshot=_json_object(
                    args.workspace_snapshot, "workspace-snapshot"
                ),
                model=_json_object(args.model, "model"),
                topology=_json_object(args.topology, "topology"),
            )
            payload = {
                "status": "created",
                "case_id": case["case_id"],
                "case_dir": str(args.case_dir.resolve()),
            }
        elif args.action == "record":
            emit_progress("record-experiment", case_dir=str(args.case_dir))
            experiment = record_experiment(
                args.case_dir,
                variable=args.variable,
                hypothesis=args.hypothesis,
                expected=args.expected,
                observed=args.observed,
                conclusion=args.conclusion,
                next_step=args.next_step,
            )
            payload = {"status": "recorded", "experiment": experiment}
        elif args.action == "compare":
            comparison, output = compare_case(
                args.case_dir,
                eager_path=args.eager,
                graph_path=args.graph,
                atol=args.atol,
                rtol=args.rtol,
            )
            payload = {
                "status": comparison["status"],
                "comparison": str(output.resolve()),
                "first_divergence": comparison["first_divergence"],
            }
        else:
            emit_progress("finalize", case_dir=str(args.case_dir))
            case = finalize_case(
                args.case_dir,
                root_cause=args.root_cause,
                fix=args.fix,
                minimal_result=args.minimal_result,
                original_result=args.original_result,
                cleanup_status=args.cleanup_status,
            )
            payload = {
                "status": case["status"],
                "case_id": case["case_id"],
                "case": str(_case_path(args.case_dir).resolve()),
            }
    except (GraphDebugError, RunManifestError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
