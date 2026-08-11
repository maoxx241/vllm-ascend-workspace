#!/usr/bin/env python3
"""Plan, link, and finalize an Ascend Triton operator workflow."""

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
    transition_status,
    write_manifest,
)

SCHEMA_VERSION = 1
STAGES = ("development", "validation", "optimization")
SOURCE_KINDS = {"torch-reference", "gpu-triton", "ascend-triton"}
EXPECTED_RUN_TYPES = {
    "development": "debug",
    "validation": "correctness",
    "optimization": "performance",
}


class WorkflowError(ValueError):
    """Raised when workflow evidence violates the contract."""


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
        raise WorkflowError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise WorkflowError(f"{label} root must be an object")
    return payload


def validate_config(config: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("run_id", "op_name"):
        if not isinstance(config.get(field), str) or not config[field]:
            errors.append(f"{field} must be a non-empty string")
    source = config.get("source")
    if not isinstance(source, Mapping):
        errors.append("source must be an object")
    else:
        if source.get("kind") not in SOURCE_KINDS:
            errors.append(f"source.kind must be one of: {', '.join(sorted(SOURCE_KINDS))}")
        if not isinstance(source.get("path"), str) or not source["path"]:
            errors.append("source.path must be a non-empty string")
    target = config.get("target")
    if not isinstance(target, Mapping):
        errors.append("target must be an object")
    elif not isinstance(target.get("soc"), str) or not target["soc"]:
        errors.append("target.soc must be a non-empty string")
    required = config.get("required_stages")
    if not isinstance(required, list) or not required:
        errors.append("required_stages must be a non-empty array")
        required = []
    elif len(set(required)) != len(required):
        errors.append("required_stages must not contain duplicates")
    unknown = sorted(set(required) - set(STAGES))
    if unknown:
        errors.append(f"unknown required stages: {', '.join(unknown)}")
    if "optimization" in required and "validation" not in required:
        errors.append("optimization requires validation")
    if errors:
        raise WorkflowError("; ".join(errors))


def plan(output_dir: Path, *, config_path: Path, created_at: str | None = None) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise WorkflowError(f"output directory is not empty: {output_dir}")
    config = _load_json(config_path, "workflow config")
    validate_config(config)
    timestamp = created_at or utc_now()
    output_dir.mkdir(parents=True, exist_ok=True)
    required = set(config["required_stages"])
    stage_plan = {
        "schema_version": SCHEMA_VERSION,
        "op_name": config["op_name"],
        "stages": [
            {
                "id": stage,
                "required": stage in required,
                "depends_on": ["validation"] if stage == "optimization" else [],
                "expected_run_type": EXPECTED_RUN_TYPES[stage],
                "status": "pending",
            }
            for stage in STAGES
            if stage in required
        ],
    }
    links = {"schema_version": SCHEMA_VERSION, "links": []}
    _write_json(output_dir / "workflow-config.json", config)
    _write_json(output_dir / "stage-plan.json", stage_plan)
    _write_json(output_dir / "evidence-links.json", links)
    manifest = new_manifest(
        run_type="change-validation",
        run_id=config["run_id"],
        workspace_snapshot=config.get("workspace_snapshot", {}),
        environment=config.get("environment", {}),
        topology={"target": config["target"]},
        command=config.get("command", []),
        created_at=timestamp,
    )
    for name, kind, uri in (
        ("workflow-config", "workflow-config", "workflow-config.json"),
        ("stage-plan", "stage-plan", "stage-plan.json"),
        ("evidence-links", "evidence-links", "evidence-links.json"),
    ):
        manifest = add_artifact(manifest, name=name, kind=kind, uri=uri, updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {"status": "planned", "run_id": config["run_id"], "stages": config["required_stages"]}


def link(output_dir: Path, *, stage: str, child_path: Path, updated_at: str | None = None) -> dict[str, Any]:
    plan_doc = _load_json(output_dir / "stage-plan.json", "stage plan")
    links = _load_json(output_dir / "evidence-links.json", "evidence links")
    stage_row = next((row for row in plan_doc["stages"] if row["id"] == stage), None)
    if stage_row is None:
        raise WorkflowError(f"stage is not planned: {stage}")
    if any(row["stage"] == stage for row in links["links"]):
        raise WorkflowError(f"stage already linked: {stage}")
    parent = load_manifest(output_dir / "manifest.json")
    child = load_manifest(child_path)
    if child["parent_run_id"] != parent["run_id"]:
        raise WorkflowError("child parent_run_id does not match workflow run_id")
    if child["run_type"] != stage_row["expected_run_type"]:
        raise WorkflowError(
            f"stage {stage} requires run_type {stage_row['expected_run_type']}, got {child['run_type']}"
        )
    if child["status"] not in TERMINAL_STATUSES:
        raise WorkflowError("child manifest must be terminal before linking")
    timestamp = updated_at or utc_now()
    links["links"].append(
        {
            "stage": stage,
            "run_id": child["run_id"],
            "run_type": child["run_type"],
            "status": child["status"],
            "manifest": str(child_path.resolve()),
            "artifacts": child["artifacts"],
            "linked_at": timestamp,
        }
    )
    stage_row["status"] = "linked"
    _write_json(output_dir / "evidence-links.json", links)
    _write_json(output_dir / "stage-plan.json", plan_doc)
    if parent["status"] == "planned":
        parent = transition_status(parent, "running", updated_at=timestamp)
        write_manifest(output_dir / "manifest.json", parent)
    return {"status": "linked", "stage": stage, "child_status": child["status"]}


def _render_report(summary: Mapping[str, Any]) -> str:
    lines = [
        "# Ascend Triton workflow report",
        "",
        f"- Status: **{summary['status']}**",
        f"- Operator: `{summary['op_name']}`",
        f"- Missing required stages: {', '.join(summary['missing_required']) or 'none'}",
        f"- Failed required stages: {', '.join(summary['failed_required']) or 'none'}",
        f"- Inconclusive required stages: {', '.join(summary['inconclusive_required']) or 'none'}",
        "",
        "## Stage evidence",
        "",
        "```json",
        json.dumps(summary["stages"], ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(lines)


def finalize(output_dir: Path, *, updated_at: str | None = None) -> dict[str, Any]:
    config = _load_json(output_dir / "workflow-config.json", "workflow config")
    plan_doc = _load_json(output_dir / "stage-plan.json", "stage plan")
    links = _load_json(output_dir / "evidence-links.json", "evidence links")
    link_by_stage = {row["stage"]: row for row in links["links"]}
    required = [row["id"] for row in plan_doc["stages"] if row["required"]]
    missing = [stage for stage in required if stage not in link_by_stage]
    failed = [stage for stage in required if link_by_stage.get(stage, {}).get("status") == "failed"]
    inconclusive = [
        stage
        for stage in required
        if link_by_stage.get(stage, {}).get("status") in {"inconclusive", "cancelled"}
    ]
    if failed:
        status = "failed"
    elif missing or inconclusive:
        status = "inconclusive"
    else:
        status = "passed"
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "op_name": config["op_name"],
        "missing_required": missing,
        "failed_required": failed,
        "inconclusive_required": inconclusive,
        "stages": links["links"],
    }
    _write_json(output_dir / "workflow-summary.json", summary)
    _atomic_write(output_dir / "workflow-report.md", _render_report(summary))
    timestamp = updated_at or utc_now()
    manifest = load_manifest(output_dir / "manifest.json")
    if manifest["status"] == "planned":
        manifest = transition_status(manifest, "running", updated_at=timestamp)
    for name, kind, uri in (
        ("workflow-summary", "summary", "workflow-summary.json"),
        ("workflow-report", "report", "workflow-report.md"),
    ):
        manifest = add_artifact(manifest, name=name, kind=kind, uri=uri, updated_at=timestamp)
    manifest = transition_status(manifest, status, updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {"status": status, "summary": str((output_dir / "workflow-summary.json").resolve()), "report": str((output_dir / "workflow-report.md").resolve())}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--output-dir", required=True, type=Path)
    plan_parser.add_argument("--config", required=True, type=Path)
    link_parser = subparsers.add_parser("link")
    link_parser.add_argument("--output-dir", required=True, type=Path)
    link_parser.add_argument("--stage", required=True, choices=STAGES)
    link_parser.add_argument("--manifest", required=True, type=Path)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "plan":
            payload = plan(args.output_dir, config_path=args.config)
        elif args.action == "link":
            payload = link(args.output_dir, stage=args.stage, child_path=args.manifest)
        else:
            payload = finalize(args.output_dir)
    except (WorkflowError, RunManifestError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
