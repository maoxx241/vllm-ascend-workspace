#!/usr/bin/env python3
"""Plan, record, and analyze isolated Ascend operator debug matrices."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from collections import defaultdict
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

SCHEMA_VERSION = 1
MODES = {"eager", "compile", "graph"}
RESULT_STATUSES = {"passed", "numerical_mismatch", "crash", "unsupported"}


class OperatorDebugError(ValueError):
    """Raised when an operator case or result violates the contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, payload: Any) -> None:
    _atomic_write(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperatorDebugError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise OperatorDebugError(f"{label} root must be an object")
    return payload


def validate_input_spec(spec: Mapping[str, Any], path: str, errors: list[str]) -> None:
    if not isinstance(spec.get("name"), str) or not spec["name"]:
        errors.append(f"{path}.name must be a non-empty string")
    shape = spec.get("shape")
    if not isinstance(shape, list) or any(
        not isinstance(size, int) or isinstance(size, bool) or size < 0
        for size in shape
    ):
        errors.append(f"{path}.shape must be an array of non-negative integers")
    for field in ("dtype", "layout"):
        if not isinstance(spec.get(field), str) or not spec[field]:
            errors.append(f"{path}.{field} must be a non-empty string")
    strides = spec.get("strides")
    if strides is not None and (
        not isinstance(strides, list)
        or any(
            not isinstance(stride, int) or isinstance(stride, bool) or stride < 0
            for stride in strides
        )
    ):
        errors.append(f"{path}.strides must be an array of non-negative integers")


def validate_config(config: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(config.get("run_id"), str) or not config["run_id"]:
        errors.append("run_id must be a non-empty string")
    operator = config.get("operator")
    if not isinstance(operator, Mapping):
        errors.append("operator must be an object")
    else:
        for field in ("name", "invocation", "reference"):
            if not isinstance(operator.get(field), str) or not operator[field]:
                errors.append(f"operator.{field} must be a non-empty string")
    tolerance = config.get("tolerance")
    if not isinstance(tolerance, Mapping):
        errors.append("tolerance must be an object")
    else:
        for field in ("atol", "rtol"):
            value = tolerance.get(field)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
            ):
                errors.append(f"tolerance.{field} must be non-negative")
    cases = config.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty array")
        cases = []
    case_ids: set[str] = set()
    for index, case in enumerate(cases):
        path = f"cases[{index}]"
        if not isinstance(case, Mapping):
            errors.append(f"{path} must be an object")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"{path}.id must be a non-empty string")
        elif case_id in case_ids:
            errors.append(f"case id is duplicated: {case_id}")
        else:
            case_ids.add(case_id)
        if case.get("mode") not in MODES:
            errors.append(f"{path}.mode must be one of: {', '.join(sorted(MODES))}")
        inputs = case.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            errors.append(f"{path}.inputs must be a non-empty array")
        else:
            for input_index, spec in enumerate(inputs):
                if not isinstance(spec, Mapping):
                    errors.append(f"{path}.inputs[{input_index}] must be an object")
                else:
                    validate_input_spec(
                        spec, f"{path}.inputs[{input_index}]", errors
                    )
        attributes = case.get("attributes", {})
        if not isinstance(attributes, Mapping):
            errors.append(f"{path}.attributes must be an object")
    if errors:
        raise OperatorDebugError("; ".join(errors))


def validate_result(result: Mapping[str, Any], planned_ids: set[str]) -> None:
    errors: list[str] = []
    if result.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if result.get("case_id") not in planned_ids:
        errors.append("case_id must identify a planned case")
    status = result.get("status")
    if status not in RESULT_STATUSES:
        errors.append(
            f"status must be one of: {', '.join(sorted(RESULT_STATUSES))}"
        )
    comparisons = result.get("comparisons", [])
    if not isinstance(comparisons, list):
        errors.append("comparisons must be an array")
        comparisons = []
    for index, comparison in enumerate(comparisons):
        if not isinstance(comparison, Mapping):
            errors.append(f"comparisons[{index}] must be an object")
            continue
        if not isinstance(comparison.get("output"), str) or not comparison["output"]:
            errors.append(f"comparisons[{index}].output must be a non-empty string")
        for metric in ("max_abs", "max_rel", "cosine"):
            value = comparison.get(metric)
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                errors.append(f"comparisons[{index}].{metric} must be finite")
    if status in {"crash", "unsupported"} and (
        not isinstance(result.get("error"), str) or not result["error"]
    ):
        errors.append(f"{status} result requires error")
    timing = result.get("timing_us")
    if timing is not None and (
        not isinstance(timing, (int, float))
        or isinstance(timing, bool)
        or not math.isfinite(float(timing))
        or timing < 0
    ):
        errors.append("timing_us must be a non-negative finite number")
    if errors:
        raise OperatorDebugError("; ".join(errors))


def plan(
    output_dir: Path, *, config_path: Path, created_at: str | None = None
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise OperatorDebugError(f"output directory is not empty: {output_dir}")
    config = _load_json(config_path, "operator config")
    validate_config(config)
    timestamp = created_at or utc_now()
    output_dir.mkdir(parents=True, exist_ok=True)
    for directory in ("raw-results", "input-metadata", "artifacts"):
        (output_dir / directory).mkdir()
    matrix = {
        "schema_version": SCHEMA_VERSION,
        "operator": config["operator"]["name"],
        "cases": [
            {
                **case,
                "status": "pending",
            }
            for case in config["cases"]
        ],
    }
    _write_json(output_dir / "operator-config.json", config)
    _write_json(output_dir / "case-matrix.json", matrix)
    _write_json(
        output_dir / "results.json",
        {"schema_version": SCHEMA_VERSION, "results": []},
    )
    _atomic_write(
        output_dir / "reproduction.md",
        "# Operator reproduction\n\n"
        f"- Operator: `{config['operator']['name']}`\n"
        f"- Invocation: `{config['operator']['invocation']}`\n"
        f"- Reference: `{config['operator']['reference']}`\n\n"
        "Execute every explicit case on a remote Ascend NPU without implicit casts.\n",
    )
    manifest = new_manifest(
        run_type="debug",
        run_id=config["run_id"],
        workspace_snapshot=config.get("workspace_snapshot", {}),
        environment=config.get("environment", {}),
        model=config.get("model", {}),
        command=config.get("command", []),
        created_at=timestamp,
    )
    for name, kind, uri in (
        ("operator-config", "operator-config", "operator-config.json"),
        ("case-matrix", "case-matrix", "case-matrix.json"),
        ("results", "operator-results", "results.json"),
        ("reproduction", "reproduction", "reproduction.md"),
    ):
        manifest = add_artifact(
            manifest, name=name, kind=kind, uri=uri, updated_at=timestamp
        )
    write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": "planned",
        "run_id": config["run_id"],
        "operator": config["operator"]["name"],
        "case_count": len(config["cases"]),
    }


def record(
    output_dir: Path, *, result_path: Path, recorded_at: str | None = None
) -> dict[str, Any]:
    matrix = _load_json(output_dir / "case-matrix.json", "case matrix")
    results = _load_json(output_dir / "results.json", "results")
    planned_ids = {case["id"] for case in matrix["cases"]}
    result = _load_json(result_path, "operator result")
    validate_result(result, planned_ids)
    if any(row["case_id"] == result["case_id"] for row in results["results"]):
        raise OperatorDebugError(f"case already recorded: {result['case_id']}")
    case = next(row for row in matrix["cases"] if row["id"] == result["case_id"])
    if case["status"] != "pending":
        raise OperatorDebugError(f"case is not pending: {result['case_id']}")
    timestamp = recorded_at or utc_now()
    normalized = {
        **result,
        "comparisons": [
            {
                key: float(value) if key in {"max_abs", "max_rel", "cosine"} else value
                for key, value in comparison.items()
            }
            for comparison in result.get("comparisons", [])
        ],
        "timing_us": (
            float(result["timing_us"]) if result.get("timing_us") is not None else None
        ),
        "source": result.get("source", str(result_path.resolve())),
        "recorded_at": timestamp,
    }
    results["results"].append(normalized)
    case["status"] = "recorded"
    _write_json(output_dir / "results.json", results)
    _write_json(output_dir / "case-matrix.json", matrix)
    manifest = load_manifest(output_dir / "manifest.json")
    if manifest["status"] == "planned":
        manifest = transition_status(manifest, "running", updated_at=timestamp)
        write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": "recorded",
        "case_id": result["case_id"],
        "remaining": sum(case["status"] == "pending" for case in matrix["cases"]),
    }


def _case_axes(case: Mapping[str, Any]) -> dict[str, list[str]]:
    axes: dict[str, list[str]] = {
        "mode": [str(case["mode"])],
        "dtype": sorted({str(spec["dtype"]) for spec in case["inputs"]}),
        "layout": sorted({str(spec["layout"]) for spec in case["inputs"]}),
        "shape": [
            ",".join("x".join(str(size) for size in spec["shape"]) for spec in case["inputs"])
        ],
    }
    for key, value in sorted(case.get("attributes", {}).items()):
        axes[f"attribute:{key}"] = [json.dumps(value, sort_keys=True)]
    return axes


def analyze_documents(
    config: Mapping[str, Any],
    matrix: Mapping[str, Any],
    results: Mapping[str, Any],
) -> dict[str, Any]:
    validate_config(config)
    result_by_id = {row["case_id"]: row for row in results["results"]}
    missing = [case["id"] for case in matrix["cases"] if case["id"] not in result_by_id]
    status_counts = {status: 0 for status in sorted(RESULT_STATUSES)}
    failure_axes: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for case in matrix["cases"]:
        result = result_by_id.get(case["id"])
        if result is None:
            continue
        status_counts[result["status"]] += 1
        if result["status"] in {"numerical_mismatch", "crash"}:
            for axis, values in _case_axes(case).items():
                for value in values:
                    failure_axes[axis][value].append(case["id"])
    crashes = sorted(
        row["case_id"] for row in results["results"] if row["status"] == "crash"
    )
    mismatches = sorted(
        row["case_id"]
        for row in results["results"]
        if row["status"] == "numerical_mismatch"
    )
    unsupported = sorted(
        row["case_id"]
        for row in results["results"]
        if row["status"] == "unsupported"
    )
    if missing:
        status = "inconclusive"
    elif crashes or mismatches:
        status = "diagnosed"
    elif unsupported:
        status = "inconclusive"
    else:
        status = "passed"
    integration_hint = (
        "isolated-operator-cases-pass-check-integration-boundary"
        if status == "passed" and bool(config.get("source_model_failure"))
        else None
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "operator": config["operator"]["name"],
        "missing_cases": missing,
        "crash_cases": crashes,
        "mismatch_cases": mismatches,
        "unsupported_cases": unsupported,
        "status_counts": status_counts,
        "failure_axes": {
            axis: {value: sorted(ids) for value, ids in sorted(values.items())}
            for axis, values in sorted(failure_axes.items())
        },
        "integration_hint": integration_hint,
        "results": results["results"],
    }


def render_report(analysis: Mapping[str, Any]) -> str:
    lines = [
        "# Ascend operator debug report",
        "",
        f"- Status: **{analysis['status']}**",
        f"- Operator: `{analysis['operator']}`",
        f"- Missing: {', '.join(analysis['missing_cases']) or 'none'}",
        f"- Crashes: {', '.join(analysis['crash_cases']) or 'none'}",
        f"- Numerical mismatches: {', '.join(analysis['mismatch_cases']) or 'none'}",
        f"- Unsupported: {', '.join(analysis['unsupported_cases']) or 'none'}",
        "",
        "## Failure axes",
        "",
        "```json",
        json.dumps(analysis["failure_axes"], ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
    ]
    if analysis["integration_hint"]:
        lines.extend(
            [
                "## Integration boundary",
                "",
                "All isolated cases passed while a model-level failure remains. "
                "Inspect caller-provided metadata, aliasing, stream/lifetime, graph "
                "integration, and post-operator consumers before changing the operator.",
                "",
            ]
        )
    lines.extend(["## Results", "", "```json"])
    lines.append(
        json.dumps(analysis["results"], ensure_ascii=False, indent=2, sort_keys=True)
    )
    lines.extend(["```", ""])
    return "\n".join(lines)


def analyze(
    output_dir: Path, *, updated_at: str | None = None
) -> dict[str, Any]:
    config = _load_json(output_dir / "operator-config.json", "operator config")
    matrix = _load_json(output_dir / "case-matrix.json", "case matrix")
    results = _load_json(output_dir / "results.json", "results")
    analysis = analyze_documents(config, matrix, results)
    _write_json(output_dir / "analysis.json", analysis)
    _atomic_write(output_dir / "report.md", render_report(analysis))
    timestamp = updated_at or utc_now()
    manifest = load_manifest(output_dir / "manifest.json")
    if manifest["status"] == "planned":
        manifest = transition_status(manifest, "running", updated_at=timestamp)
    for name, kind, uri in (
        ("analysis", "analysis", "analysis.json"),
        ("report", "report", "report.md"),
    ):
        manifest = add_artifact(
            manifest, name=name, kind=kind, uri=uri, updated_at=timestamp
        )
    terminal = {
        "passed": "passed",
        "diagnosed": "failed",
        "inconclusive": "inconclusive",
    }[analysis["status"]]
    manifest = transition_status(manifest, terminal, updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": analysis["status"],
        "crash_cases": analysis["crash_cases"],
        "mismatch_cases": analysis["mismatch_cases"],
        "missing_cases": analysis["missing_cases"],
        "analysis": str((output_dir / "analysis.json").resolve()),
        "report": str((output_dir / "report.md").resolve()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan_parser = subparsers.add_parser("plan", help="create an operator case")
    plan_parser.add_argument("--output-dir", required=True, type=Path)
    plan_parser.add_argument("--config", required=True, type=Path)
    record_parser = subparsers.add_parser("record", help="record a planned case")
    record_parser.add_argument("--output-dir", required=True, type=Path)
    record_parser.add_argument("--result", required=True, type=Path)
    analyze_parser = subparsers.add_parser("analyze", help="analyze the case matrix")
    analyze_parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "plan":
            payload = plan(args.output_dir, config_path=args.config)
        elif args.action == "record":
            payload = record(args.output_dir, result_path=args.result)
        else:
            payload = analyze(args.output_dir)
    except (OperatorDebugError, RunManifestError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
