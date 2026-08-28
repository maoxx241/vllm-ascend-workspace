#!/usr/bin/env python3
"""Plan, record, and analyze Ascend Triton correctness evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from validate_triton_impl import analyze_file  # noqa: E402
from vaws_run_manifest import (  # noqa: E402
    RunManifestError,
    add_artifact,
    load_manifest,
    new_manifest,
    sha256_file,
    transition_status,
    write_manifest,
)

SCHEMA_VERSION = 1
MODES = {"eager", "compile", "graph"}
RESULT_STATUSES = {"passed", "numerical_mismatch", "compilation_error", "runtime_error", "unsupported"}


class ValidationError(ValueError):
    """Raised when validation evidence violates the contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
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
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"{label} root must be an object")
    return payload


def _validate_input(spec: Mapping[str, Any], path: str, errors: list[str]) -> None:
    if not isinstance(spec.get("name"), str) or not spec["name"]:
        errors.append(f"{path}.name must be a non-empty string")
    shape = spec.get("shape")
    if not isinstance(shape, list) or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in shape):
        errors.append(f"{path}.shape must contain non-negative integers")
    for field in ("dtype", "layout"):
        if not isinstance(spec.get(field), str) or not spec[field]:
            errors.append(f"{path}.{field} must be a non-empty string")
    strides = spec.get("strides")
    if strides is not None and (not isinstance(strides, list) or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in strides)):
        errors.append(f"{path}.strides must contain non-negative integers")


def validate_config(config: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("run_id", "op_name"):
        if not isinstance(config.get(field), str) or not config[field]:
            errors.append(f"{field} must be a non-empty string")
    reference = config.get("reference")
    if not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str) or not reference.get("path"):
        errors.append("reference.path must be a non-empty string")
    target = config.get("target")
    if not isinstance(target, Mapping) or not isinstance(target.get("soc"), str) or not target.get("soc"):
        errors.append("target.soc must be a non-empty string")
    tolerances = config.get("tolerances")
    if not isinstance(tolerances, Mapping) or not tolerances:
        errors.append("tolerances must be a non-empty object")
        tolerances = {}
    cases = config.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty array")
        cases = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        path = f"cases[{index}]"
        if not isinstance(case, Mapping):
            errors.append(f"{path} must be an object")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"{path}.id must be a non-empty string")
        elif case_id in seen:
            errors.append(f"case id is duplicated: {case_id}")
        else:
            seen.add(case_id)
        if case.get("mode") not in MODES:
            errors.append(f"{path}.mode must be one of: {', '.join(sorted(MODES))}")
        inputs = case.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            errors.append(f"{path}.inputs must be a non-empty array")
        else:
            for input_index, spec in enumerate(inputs):
                if not isinstance(spec, Mapping):
                    errors.append(f"{path}.inputs[{input_index}] must be an object")
                    continue
                _validate_input(spec, f"{path}.inputs[{input_index}]", errors)
                if spec.get("dtype") not in tolerances:
                    errors.append(f"no tolerance declared for dtype {spec.get('dtype')}")
    if errors:
        raise ValidationError("; ".join(errors))


def validate_result(result: Mapping[str, Any], planned_ids: set[str]) -> None:
    errors: list[str] = []
    if result.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if result.get("case_id") not in planned_ids:
        errors.append("case_id must identify a planned case")
    status = result.get("status")
    if status not in RESULT_STATUSES:
        errors.append(f"status must be one of: {', '.join(sorted(RESULT_STATUSES))}")
    if status != "passed" and (not isinstance(result.get("error"), str) or not result["error"]):
        errors.append(f"{status} result requires error")
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
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value))):
                errors.append(f"comparisons[{index}].{metric} must be finite")
    if errors:
        raise ValidationError("; ".join(errors))


def plan(output_dir: Path, *, config_path: Path, kernel: Path, created_at: str | None = None) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValidationError(f"output directory is not empty: {output_dir}")
    if not kernel.is_file():
        raise ValidationError(f"kernel does not exist: {kernel}")
    config = _load_json(config_path, "validation config")
    validate_config(config)
    static_check = analyze_file(kernel)
    if not static_check.get("valid"):
        raise ValidationError(f"static Triton gate failed: {json.dumps(static_check, ensure_ascii=False)}")
    timestamp = created_at or utc_now()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "raw-results").mkdir()
    _write_json(output_dir / "validation-config.json", config)
    _write_json(output_dir / "static-check.json", static_check)
    _write_json(
        output_dir / "case-matrix.json",
        {"schema_version": SCHEMA_VERSION, "kernel": {"path": str(kernel.resolve()), "sha256": sha256_file(kernel)}, "cases": [{**case, "status": "pending"} for case in config["cases"]]},
    )
    _write_json(output_dir / "results.json", {"schema_version": SCHEMA_VERSION, "results": []})
    manifest = new_manifest(
        run_type="correctness",
        run_id=config["run_id"],
        parent_run_id=config.get("parent_run_id"),
        workspace_snapshot=config.get("workspace_snapshot", {}),
        environment=config.get("environment", {}),
        topology={"target": config["target"]},
        command=config.get("command", []),
        created_at=timestamp,
    )
    for name, kind, uri, digest in (
        ("kernel", "triton-kernel", str(kernel.resolve()), sha256_file(kernel)),
        ("validation-config", "validation-config", "validation-config.json", None),
        ("static-check", "static-check", "static-check.json", None),
        ("case-matrix", "case-matrix", "case-matrix.json", None),
        ("results", "validation-results", "results.json", None),
    ):
        manifest = add_artifact(manifest, name=name, kind=kind, uri=uri, sha256=digest, updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {"status": "planned", "run_id": config["run_id"], "case_count": len(config["cases"]), "kernel_sha256": sha256_file(kernel)}


def record(output_dir: Path, *, result_path: Path, recorded_at: str | None = None) -> dict[str, Any]:
    matrix = _load_json(output_dir / "case-matrix.json", "case matrix")
    results = _load_json(output_dir / "results.json", "results")
    planned_ids = {case["id"] for case in matrix["cases"]}
    result = _load_json(result_path, "case result")
    validate_result(result, planned_ids)
    if any(row["case_id"] == result["case_id"] for row in results["results"]):
        raise ValidationError(f"case already recorded: {result['case_id']}")
    case = next(row for row in matrix["cases"] if row["id"] == result["case_id"])
    timestamp = recorded_at or utc_now()
    normalized = {**result, "source": result.get("source", str(result_path.resolve())), "recorded_at": timestamp}
    results["results"].append(normalized)
    case["status"] = "recorded"
    _write_json(output_dir / "results.json", results)
    _write_json(output_dir / "case-matrix.json", matrix)
    manifest = load_manifest(output_dir / "manifest.json")
    if manifest["status"] == "planned":
        manifest = transition_status(manifest, "running", updated_at=timestamp)
        write_manifest(output_dir / "manifest.json", manifest)
    return {"status": "recorded", "case_id": result["case_id"], "remaining": sum(row["status"] == "pending" for row in matrix["cases"])}


def analyze(output_dir: Path, *, updated_at: str | None = None) -> dict[str, Any]:
    config = _load_json(output_dir / "validation-config.json", "validation config")
    matrix = _load_json(output_dir / "case-matrix.json", "case matrix")
    results = _load_json(output_dir / "results.json", "results")
    result_by_id = {row["case_id"]: row for row in results["results"]}
    missing = [case["id"] for case in matrix["cases"] if case["id"] not in result_by_id]
    counts = Counter(row["status"] for row in results["results"])
    failure_statuses = {"numerical_mismatch", "compilation_error", "runtime_error"}
    failures = sorted(row["case_id"] for row in results["results"] if row["status"] in failure_statuses)
    unsupported = sorted(row["case_id"] for row in results["results"] if row["status"] == "unsupported")
    passed = sorted(row["case_id"] for row in results["results"] if row["status"] == "passed")
    if failures:
        status = "failed"
    elif missing or unsupported:
        status = "inconclusive"
    else:
        status = "passed"
    analysis = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "op_name": config["op_name"],
        "total_cases": len(matrix["cases"]),
        "passed_cases": len(passed),
        "missing_cases": missing,
        "failed_cases": failures,
        "unsupported_cases": unsupported,
        "status_counts": dict(sorted(counts.items())),
        "results": results["results"],
    }
    _write_json(output_dir / "analysis.json", analysis)
    report = (
        "# Ascend Triton validation report\n\n"
        f"- Status: **{status}**\n"
        f"- Operator: `{config['op_name']}`\n"
        f"- Passed: {len(passed)} / {len(matrix['cases'])}\n"
        f"- Missing: {', '.join(missing) or 'none'}\n"
        f"- Failed: {', '.join(failures) or 'none'}\n"
        f"- Unsupported: {', '.join(unsupported) or 'none'}\n"
    )
    _atomic_write(output_dir / "report.md", report)
    timestamp = updated_at or utc_now()
    manifest = load_manifest(output_dir / "manifest.json")
    if manifest["status"] == "planned":
        manifest = transition_status(manifest, "running", updated_at=timestamp)
    for name, kind, uri in (("analysis", "analysis", "analysis.json"), ("report", "report", "report.md")):
        manifest = add_artifact(manifest, name=name, kind=kind, uri=uri, updated_at=timestamp)
    manifest = transition_status(manifest, status, updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {"status": status, "passed_cases": len(passed), "total_cases": len(matrix["cases"]), "analysis": str((output_dir / "analysis.json").resolve())}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--output-dir", required=True, type=Path)
    plan_parser.add_argument("--config", required=True, type=Path)
    plan_parser.add_argument("--kernel", required=True, type=Path)
    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--output-dir", required=True, type=Path)
    record_parser.add_argument("--result", required=True, type=Path)
    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "plan":
            payload = plan(args.output_dir, config_path=args.config, kernel=args.kernel)
        elif args.action == "record":
            payload = record(args.output_dir, result_path=args.result)
        else:
            payload = analyze(args.output_dir)
    except (ValidationError, RunManifestError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
