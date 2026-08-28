#!/usr/bin/env python3
"""Plan and finalize an Ascend Triton development or migration run."""

from __future__ import annotations

import argparse
import json
import os
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
    TERMINAL_STATUSES,
    add_artifact,
    load_manifest,
    new_manifest,
    sha256_file,
    transition_status,
    write_manifest,
)

SCHEMA_VERSION = 1
MODES = {"direct", "gpu-migration"}
SOURCE_KINDS = {"torch-reference", "gpu-triton"}


class DevelopmentError(ValueError):
    """Raised when development evidence violates the contract."""


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
        raise DevelopmentError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DevelopmentError(f"{label} root must be an object")
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
    if config.get("mode") not in MODES:
        errors.append(f"mode must be one of: {', '.join(sorted(MODES))}")
    source = config.get("source")
    if not isinstance(source, Mapping):
        errors.append("source must be an object")
    else:
        if source.get("kind") not in SOURCE_KINDS:
            errors.append(f"source.kind must be one of: {', '.join(sorted(SOURCE_KINDS))}")
        if not isinstance(source.get("path"), str) or not source["path"]:
            errors.append("source.path must be a non-empty string")
    reference = config.get("reference")
    if not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str) or not reference.get("path"):
        errors.append("reference.path must be a non-empty string")
    target = config.get("target")
    if not isinstance(target, Mapping) or not isinstance(target.get("soc"), str) or not target.get("soc"):
        errors.append("target.soc must be a non-empty string")
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
        inputs = case.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            errors.append(f"{path}.inputs must be a non-empty array")
        else:
            for input_index, spec in enumerate(inputs):
                if not isinstance(spec, Mapping):
                    errors.append(f"{path}.inputs[{input_index}] must be an object")
                else:
                    _validate_input(spec, f"{path}.inputs[{input_index}]", errors)
    if not isinstance(config.get("tolerances"), Mapping) or not config.get("tolerances"):
        errors.append("tolerances must be a non-empty object")
    if errors:
        raise DevelopmentError("; ".join(errors))


def plan(output_dir: Path, *, config_path: Path, created_at: str | None = None) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise DevelopmentError(f"output directory is not empty: {output_dir}")
    config = _load_json(config_path, "development config")
    validate_config(config)
    timestamp = created_at or utc_now()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "candidates").mkdir()
    (output_dir / "artifacts").mkdir()
    contract = {
        "schema_version": SCHEMA_VERSION,
        "op_name": config["op_name"],
        "mode": config["mode"],
        "source": config["source"],
        "reference": config["reference"],
        "target": config["target"],
        "tolerances": config["tolerances"],
        "cases": config["cases"],
    }
    _write_json(output_dir / "task-config.json", config)
    _write_json(output_dir / "task-contract.json", contract)
    _atomic_write(
        output_dir / "semantic-report.md",
        "# Semantic report\n\n"
        "## Contract\n\n- [ ] Inputs, outputs, dynamic ranges, layouts, strides, and side effects recorded.\n\n"
        "## Loads and stores\n\n- [ ] Every load/store pointer and mask explained.\n\n"
        "## Padding and numerical semantics\n\n- [ ] Identities, accumulation dtype, atomics, aliases, and tail behavior explained.\n\n"
        "## Grid mapping\n\n- [ ] Logical tasks and target physical-core mapping explained.\n",
    )
    _atomic_write(
        output_dir / "sketch.md",
        "# Kernel sketch\n\n"
        "## Logical work\n\n"
        "## Grid and per-core scheduling\n\n"
        "## Tiling and UB peak live set\n\n"
        "## Masks, padding, dtype, and specialization\n\n",
    )
    manifest = new_manifest(
        run_type="debug",
        run_id=config["run_id"],
        parent_run_id=config.get("parent_run_id"),
        workspace_snapshot=config.get("workspace_snapshot", {}),
        environment=config.get("environment", {}),
        topology={"target": config["target"]},
        command=config.get("command", []),
        created_at=timestamp,
    )
    for name, kind, uri in (
        ("task-config", "development-config", "task-config.json"),
        ("task-contract", "task-contract", "task-contract.json"),
    ):
        manifest = add_artifact(manifest, name=name, kind=kind, uri=uri, updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {"status": "planned", "run_id": config["run_id"], "case_count": len(config["cases"])}


def _require_nonempty(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise DevelopmentError(f"{label} must be a non-empty file: {path}")


def _artifact(manifest: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    artifact = next((item for item in manifest["artifacts"] if item["name"] == name), None)
    if artifact is None:
        raise DevelopmentError(f"validation manifest is missing {name} artifact")
    return artifact


def _artifact_path(manifest_path: Path, artifact: Mapping[str, Any]) -> Path:
    path = Path(str(artifact["uri"]))
    return path if path.is_absolute() else manifest_path.parent / path


def finalize(
    output_dir: Path,
    *,
    kernel: Path,
    semantic_report: Path,
    sketch: Path,
    validation_manifest: Path,
    updated_at: str | None = None,
) -> dict[str, Any]:
    for path, label in ((kernel, "kernel"), (semantic_report, "semantic report"), (sketch, "sketch")):
        _require_nonempty(path, label)
    manifest = load_manifest(output_dir / "manifest.json")
    validation = load_manifest(validation_manifest)
    if validation["run_type"] != "correctness":
        raise DevelopmentError("validation manifest must use run_type correctness")
    valid_parents = {manifest["run_id"], manifest["parent_run_id"]} - {None}
    if validation["parent_run_id"] not in valid_parents:
        raise DevelopmentError("validation parent_run_id must identify this run or its parent workflow")
    if validation["status"] not in TERMINAL_STATUSES:
        raise DevelopmentError("validation manifest must be terminal")
    kernel_hash = sha256_file(kernel)
    kernel_artifact = _artifact(validation, "kernel")
    if kernel_artifact.get("sha256") != kernel_hash:
        raise DevelopmentError("validation kernel hash does not match the development candidate")
    matrix_artifact = _artifact(validation, "case-matrix")
    matrix = _load_json(_artifact_path(validation_manifest, matrix_artifact), "validation case matrix")
    matrix_kernel = matrix.get("kernel")
    if not isinstance(matrix_kernel, Mapping) or matrix_kernel.get("sha256") != kernel_hash:
        raise DevelopmentError("validation case matrix does not match the development candidate")
    config = _load_json(output_dir / "task-config.json", "development config")
    expected_cases = {case["id"] for case in config["cases"]}
    matrix_cases = matrix.get("cases")
    if not isinstance(matrix_cases, list) or any(not isinstance(case, Mapping) for case in matrix_cases):
        raise DevelopmentError("validation case matrix cases must be an array of objects")
    if {case.get("id") for case in matrix_cases} != expected_cases:
        raise DevelopmentError("validation case set does not match the development case set")
    terminal = {
        "passed": "passed",
        "failed": "failed",
        "inconclusive": "inconclusive",
        "cancelled": "inconclusive",
    }[validation["status"]]
    timestamp = updated_at or utc_now()
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": terminal,
        "kernel": {"path": str(kernel.resolve()), "sha256": kernel_hash},
        "semantic_report": {"path": str(semantic_report.resolve()), "sha256": sha256_file(semantic_report)},
        "sketch": {"path": str(sketch.resolve()), "sha256": sha256_file(sketch)},
        "validation": {
            "run_id": validation["run_id"],
            "status": validation["status"],
            "manifest": str(validation_manifest.resolve()),
        },
    }
    _write_json(output_dir / "development-result.json", result)
    if manifest["status"] == "planned":
        manifest = transition_status(manifest, "running", updated_at=timestamp)
    artifacts = (
        ("kernel", "triton-kernel", str(kernel.resolve()), kernel_hash),
        ("semantic-report", "semantic-report", str(semantic_report.resolve()), sha256_file(semantic_report)),
        ("sketch", "kernel-sketch", str(sketch.resolve()), sha256_file(sketch)),
        ("development-result", "result", "development-result.json", None),
        ("validation-manifest", "run-manifest", str(validation_manifest.resolve()), None),
    )
    for name, kind, uri, digest in artifacts:
        manifest = add_artifact(manifest, name=name, kind=kind, uri=uri, sha256=digest, updated_at=timestamp)
    report = (
        "# Ascend Triton development report\n\n"
        f"- Status: **{terminal}**\n"
        f"- Kernel: `{kernel.resolve()}`\n"
        f"- Kernel SHA256: `{result['kernel']['sha256']}`\n"
        f"- Validation: `{validation['run_id']}` ({validation['status']})\n"
    )
    _atomic_write(output_dir / "development-report.md", report)
    manifest = add_artifact(
        manifest,
        name="development-report",
        kind="report",
        uri="development-report.md",
        updated_at=timestamp,
    )
    manifest = transition_status(manifest, terminal, updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {"status": terminal, "kernel_sha256": result["kernel"]["sha256"], "result": str((output_dir / "development-result.json").resolve())}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--output-dir", required=True, type=Path)
    plan_parser.add_argument("--config", required=True, type=Path)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--output-dir", required=True, type=Path)
    finalize_parser.add_argument("--kernel", required=True, type=Path)
    finalize_parser.add_argument("--semantic-report", required=True, type=Path)
    finalize_parser.add_argument("--sketch", required=True, type=Path)
    finalize_parser.add_argument("--validation-manifest", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "plan":
            payload = plan(args.output_dir, config_path=args.config)
        else:
            payload = finalize(
                args.output_dir,
                kernel=args.kernel,
                semantic_report=args.semantic_report,
                sketch=args.sketch,
                validation_manifest=args.validation_manifest,
            )
    except (DevelopmentError, RunManifestError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
