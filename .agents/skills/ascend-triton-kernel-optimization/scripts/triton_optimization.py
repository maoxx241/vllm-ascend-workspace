#!/usr/bin/env python3
"""Plan, record, and analyze single-kernel Ascend Triton optimization rounds."""

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
    sha256_file,
    transition_status,
    write_manifest,
)

SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class OptimizationError(ValueError):
    """Raised when optimization evidence violates the contract."""


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
        raise OptimizationError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise OptimizationError(f"{label} root must be an object")
    return payload


def _artifact(manifest: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    artifact = next((item for item in manifest["artifacts"] if item["name"] == name), None)
    if artifact is None:
        raise OptimizationError(f"validation manifest is missing {name} artifact")
    return artifact


def _artifact_path(manifest_path: Path, artifact: Mapping[str, Any]) -> Path:
    path = Path(str(artifact["uri"]))
    return path if path.is_absolute() else manifest_path.parent / path


def _require_validation_coverage(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    *,
    kernel_sha256: str,
    case_ids: set[str],
) -> None:
    kernel_artifact = _artifact(manifest, "kernel")
    if kernel_artifact.get("sha256") != kernel_sha256:
        raise OptimizationError("validation kernel hash does not match the candidate")
    matrix_artifact = _artifact(manifest, "case-matrix")
    matrix = _load_json(_artifact_path(manifest_path, matrix_artifact), "validation case matrix")
    matrix_kernel = matrix.get("kernel")
    if not isinstance(matrix_kernel, Mapping) or matrix_kernel.get("sha256") != kernel_sha256:
        raise OptimizationError("validation case matrix does not match the candidate")
    rows = matrix.get("cases")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise OptimizationError("validation case matrix cases must be an array of objects")
    validated_ids = {row.get("id") for row in rows}
    if validated_ids != case_ids:
        raise OptimizationError("validation case set does not match the optimization case set")


def _finite_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value > 0


def _measurement_map(rows: Any, case_ids: set[str], label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise OptimizationError(f"{label} must be an array")
    mapped: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise OptimizationError(f"{label}[{index}] must be an object")
        case_id = row.get("case_id")
        if case_id not in case_ids:
            raise OptimizationError(f"{label}[{index}].case_id is not planned")
        if case_id in mapped:
            raise OptimizationError(f"{label} duplicates case_id {case_id}")
        if not _finite_positive(row.get("median_us")):
            raise OptimizationError(f"{label}[{index}].median_us must be positive and finite")
        repeats = row.get("repeats")
        if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1:
            raise OptimizationError(f"{label}[{index}].repeats must be a positive integer")
        mapped[str(case_id)] = {**row, "median_us": float(row["median_us"])}
    missing = sorted(case_ids - set(mapped))
    if missing:
        raise OptimizationError(f"{label} is missing cases: {', '.join(missing)}")
    return mapped


def validate_config(config: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("run_id", "op_name", "validation_manifest"):
        if not isinstance(config.get(field), str) or not config[field]:
            errors.append(f"{field} must be a non-empty string")
    kernel = config.get("kernel")
    if not isinstance(kernel, Mapping) or not isinstance(kernel.get("path"), str) or not kernel.get("path"):
        errors.append("kernel.path must be a non-empty string")
    target = config.get("target")
    if not isinstance(target, Mapping) or not isinstance(target.get("soc"), str) or not target.get("soc"):
        errors.append("target.soc must be a non-empty string")
    cases = config.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty array")
        cases = []
    case_ids: set[str] = set()
    total_weight = 0.0
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping) or not isinstance(case.get("id"), str) or not case.get("id"):
            errors.append(f"cases[{index}].id must be a non-empty string")
            continue
        if case["id"] in case_ids:
            errors.append(f"case id is duplicated: {case['id']}")
        case_ids.add(case["id"])
        weight = case.get("weight", 1.0)
        if not _finite_positive(weight):
            errors.append(f"cases[{index}].weight must be positive and finite")
        else:
            total_weight += float(weight)
    if total_weight <= 0:
        errors.append("case weights must sum to a positive value")
    objective = config.get("objective")
    if not isinstance(objective, Mapping):
        errors.append("objective must be an object")
    else:
        for field in ("target_relative_improvement", "min_relative_improvement", "noise_floor", "max_case_regression"):
            value = objective.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or value < 0:
                errors.append(f"objective.{field} must be non-negative and finite")
    for field in ("max_rounds", "max_consecutive_failures"):
        value = config.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            errors.append(f"{field} must be a positive integer")
    if errors:
        raise OptimizationError("; ".join(errors))
    _measurement_map(config.get("baseline"), case_ids, "baseline")


def plan(output_dir: Path, *, config_path: Path, created_at: str | None = None) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise OptimizationError(f"output directory is not empty: {output_dir}")
    config = _load_json(config_path, "optimization config")
    validate_config(config)
    kernel_path = Path(config["kernel"]["path"])
    if not kernel_path.is_file():
        raise OptimizationError(f"kernel does not exist: {kernel_path}")
    validation_path = Path(config["validation_manifest"])
    validation = load_manifest(validation_path)
    if validation["run_type"] != "correctness" or validation["status"] != "passed":
        raise OptimizationError("starting validation manifest must be passed correctness evidence")
    timestamp = created_at or utc_now()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "round-artifacts").mkdir()
    kernel_hash = sha256_file(kernel_path)
    case_ids = {case["id"] for case in config["cases"]}
    _require_validation_coverage(
        validation_path,
        validation,
        kernel_sha256=kernel_hash,
        case_ids=case_ids,
    )
    baseline = _measurement_map(config["baseline"], case_ids, "baseline")
    state = {
        "schema_version": SCHEMA_VERSION,
        "next_round": 1,
        "current_best_sha256": kernel_hash,
        "current_best_path": str(kernel_path.resolve()),
        "original_baseline": baseline,
        "current_best_measurements": baseline,
        "kept_rounds": [],
        "consecutive_failures": 0,
        "target_met": False,
    }
    _write_json(output_dir / "optimization-config.json", config)
    _write_json(output_dir / "state.json", state)
    _write_json(output_dir / "rounds.json", {"schema_version": SCHEMA_VERSION, "rounds": []})
    manifest = new_manifest(
        run_type="performance",
        run_id=config["run_id"],
        parent_run_id=config.get("parent_run_id"),
        workspace_snapshot=config.get("workspace_snapshot", {}),
        environment=config.get("environment", {}),
        topology={"target": config["target"]},
        command=config.get("command", []),
        created_at=timestamp,
    )
    for name, kind, uri, digest in (
        ("starting-kernel", "triton-kernel", str(kernel_path.resolve()), kernel_hash),
        ("starting-validation", "run-manifest", str(validation_path.resolve()), None),
        ("optimization-config", "optimization-config", "optimization-config.json", None),
        ("optimization-state", "state", "state.json", None),
        ("rounds", "optimization-rounds", "rounds.json", None),
    ):
        manifest = add_artifact(manifest, name=name, kind=kind, uri=uri, sha256=digest, updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {"status": "planned", "run_id": config["run_id"], "kernel_sha256": kernel_hash, "case_count": len(case_ids)}


def _weighted_improvement(current: Mapping[str, Mapping[str, Any]], candidate: Mapping[str, Mapping[str, Any]], cases: list[Mapping[str, Any]]) -> tuple[float, float]:
    total_weight = sum(float(case.get("weight", 1.0)) for case in cases)
    improvement = 0.0
    worst_regression = -math.inf
    for case in cases:
        case_id = str(case["id"])
        weight = float(case.get("weight", 1.0)) / total_weight
        old = float(current[case_id]["median_us"])
        new = float(candidate[case_id]["median_us"])
        improvement += weight * ((old - new) / old)
        worst_regression = max(worst_regression, (new - old) / old)
    return improvement, worst_regression


def record(output_dir: Path, *, result_path: Path, recorded_at: str | None = None) -> dict[str, Any]:
    config = _load_json(output_dir / "optimization-config.json", "optimization config")
    state = _load_json(output_dir / "state.json", "optimization state")
    rounds = _load_json(output_dir / "rounds.json", "rounds")
    result = _load_json(result_path, "round result")
    if result.get("schema_version") != SCHEMA_VERSION:
        raise OptimizationError(f"schema_version must be {SCHEMA_VERSION}")
    if result.get("round") != state["next_round"]:
        raise OptimizationError(f"round must be {state['next_round']}")
    if result["round"] > config["max_rounds"]:
        raise OptimizationError("round budget exhausted")
    if result.get("parent_kernel_sha256") != state["current_best_sha256"]:
        raise OptimizationError("parent_kernel_sha256 does not match current best")
    for field in ("hypothesis", "change"):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise OptimizationError(f"{field} must be a non-empty string")
    candidate = result.get("candidate")
    if not isinstance(candidate, Mapping) or not isinstance(candidate.get("path"), str) or not candidate.get("path"):
        raise OptimizationError("candidate.path must be a non-empty string")
    candidate_path = Path(candidate["path"])
    if not candidate_path.is_file():
        raise OptimizationError(f"candidate does not exist: {candidate_path}")
    candidate_hash = sha256_file(candidate_path)
    verification = result.get("verification")
    if not isinstance(verification, Mapping) or not isinstance(verification.get("manifest"), str):
        raise OptimizationError("verification.manifest must be a path")
    verification_manifest = load_manifest(Path(verification["manifest"]))
    if verification_manifest["run_type"] != "correctness":
        raise OptimizationError("round verification must use run_type correctness")
    valid_parents = {config["run_id"], config.get("parent_run_id")} - {None}
    if verification_manifest["parent_run_id"] not in valid_parents:
        raise OptimizationError("round validation parent_run_id must identify this run or its parent workflow")
    case_ids = {case["id"] for case in config["cases"]}
    _require_validation_coverage(
        Path(verification["manifest"]),
        verification_manifest,
        kernel_sha256=candidate_hash,
        case_ids=case_ids,
    )
    timestamp = recorded_at or utc_now()
    decision = "FAIL"
    improvement = None
    worst_regression = None
    cumulative = 0.0
    measurements: dict[str, dict[str, Any]] | None = None
    if verification_manifest["status"] == "passed":
        measurements = _measurement_map(result.get("measurements"), case_ids, "measurements")
        improvement, worst_regression = _weighted_improvement(state["current_best_measurements"], measurements, config["cases"])
        objective = config["objective"]
        if abs(improvement) < float(objective["noise_floor"]):
            decision = "NOISE"
        elif improvement >= float(objective["min_relative_improvement"]) and worst_regression <= float(objective["max_case_regression"]):
            decision = "KEEP"
        else:
            decision = "DISCARD"
        if decision == "KEEP":
            state["current_best_sha256"] = candidate_hash
            state["current_best_path"] = str(candidate_path.resolve())
            state["current_best_measurements"] = measurements
            state["kept_rounds"].append(result["round"])
            state["consecutive_failures"] = 0
        else:
            state["consecutive_failures"] += 1
    else:
        state["consecutive_failures"] += 1
    cumulative, _ = _weighted_improvement(state["original_baseline"], state["current_best_measurements"], config["cases"])
    state["target_met"] = cumulative >= float(config["objective"]["target_relative_improvement"])
    state["next_round"] += 1
    normalized = {
        **result,
        "candidate": {"path": str(candidate_path.resolve()), "sha256": candidate_hash},
        "verification": {"manifest": str(Path(verification["manifest"]).resolve()), "run_id": verification_manifest["run_id"], "status": verification_manifest["status"]},
        "measurements": list(measurements.values()) if measurements is not None else [],
        "decision": decision,
        "relative_improvement_vs_current": improvement,
        "worst_case_regression": worst_regression,
        "relative_improvement_vs_original": cumulative,
        "recorded_at": timestamp,
    }
    rounds["rounds"].append(normalized)
    _write_json(output_dir / "rounds.json", rounds)
    _write_json(output_dir / "state.json", state)
    manifest = load_manifest(output_dir / "manifest.json")
    if manifest["status"] == "planned":
        manifest = transition_status(manifest, "running", updated_at=timestamp)
        write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": "recorded",
        "round": result["round"],
        "decision": decision,
        "target_met": state["target_met"],
        "needs_diagnosis": state["consecutive_failures"] >= config["max_consecutive_failures"],
        "remaining_rounds": config["max_rounds"] - len(rounds["rounds"]),
    }


def analyze(output_dir: Path, *, updated_at: str | None = None) -> dict[str, Any]:
    config = _load_json(output_dir / "optimization-config.json", "optimization config")
    state = _load_json(output_dir / "state.json", "optimization state")
    rounds = _load_json(output_dir / "rounds.json", "rounds")
    exhausted = len(rounds["rounds"]) >= config["max_rounds"]
    diagnosis = state["consecutive_failures"] >= config["max_consecutive_failures"]
    if state["target_met"]:
        status = "passed"
    elif exhausted or diagnosis:
        status = "failed"
    else:
        status = "inconclusive"
    cumulative, _ = _weighted_improvement(state["original_baseline"], state["current_best_measurements"], config["cases"])
    analysis = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "op_name": config["op_name"],
        "best_kernel": {"path": state["current_best_path"], "sha256": state["current_best_sha256"]},
        "relative_improvement_vs_original": cumulative,
        "target_relative_improvement": config["objective"]["target_relative_improvement"],
        "target_met": state["target_met"],
        "kept_rounds": state["kept_rounds"],
        "round_count": len(rounds["rounds"]),
        "needs_diagnosis": diagnosis,
        "rounds": rounds["rounds"],
    }
    _write_json(output_dir / "analysis.json", analysis)
    _atomic_write(
        output_dir / "report.md",
        "# Ascend Triton optimization report\n\n"
        f"- Status: **{status}**\n"
        f"- Operator: `{config['op_name']}`\n"
        f"- Best kernel: `{state['current_best_path']}`\n"
        f"- Improvement vs original: {cumulative:.4%}\n"
        f"- Target: {float(config['objective']['target_relative_improvement']):.4%}\n"
        f"- Kept rounds: {', '.join(str(value) for value in state['kept_rounds']) or 'none'}\n"
        f"- Diagnosis required: {diagnosis}\n",
    )
    timestamp = updated_at or utc_now()
    manifest = load_manifest(output_dir / "manifest.json")
    if manifest["status"] == "planned":
        manifest = transition_status(manifest, "running", updated_at=timestamp)
    for name, kind, uri in (("analysis", "analysis", "analysis.json"), ("report", "report", "report.md")):
        manifest = add_artifact(manifest, name=name, kind=kind, uri=uri, updated_at=timestamp)
    manifest = transition_status(manifest, status, updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {"status": status, "target_met": state["target_met"], "best_kernel_sha256": state["current_best_sha256"], "analysis": str((output_dir / "analysis.json").resolve())}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--output-dir", required=True, type=Path)
    plan_parser.add_argument("--config", required=True, type=Path)
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
            payload = plan(args.output_dir, config_path=args.config)
        elif args.action == "record":
            payload = record(args.output_dir, result_path=args.result)
        else:
            payload = analyze(args.output_dir)
    except (OptimizationError, RunManifestError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
