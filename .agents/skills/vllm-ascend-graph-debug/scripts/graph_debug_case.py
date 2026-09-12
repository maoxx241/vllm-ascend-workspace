#!/usr/bin/env python3
"""Compare collected graph/eager JSONL snapshots and their execution identities."""

from __future__ import annotations

import argparse
import json
import math
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

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)


from vaws_comparability import (  # noqa: E402
    GRAPH_MUST_OBSERVE,
    ComparabilityError,
    consume_certificate,
    identity_from_mapping,
    issue_certificate,
)
from vaws_coordinator.run_manifest import (  # noqa: E402
    RunManifestError,
    add_artifact,
    new_manifest,
    transition_status,
    write_manifest,
)

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
    for field in ("shape", "dtype", "stride", "layout"):
        if eager.get(field) != graph.get(field):
            differences.append({"field": field, "reason": "metadata-mismatch",
                                "eager": eager.get(field), "graph": graph.get(field)})
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
    if not math.isfinite(atol) or not math.isfinite(rtol) or atol < 0 or rtol < 0:
        raise GraphDebugError("atol and rtol must be finite and non-negative")
    eager = load_snapshots(eager_path)
    graph = load_snapshots(graph_path)
    eager_keys = set(eager)
    graph_keys = set(graph)
    missing_in_graph = sorted(eager_keys - graph_keys)
    missing_in_eager = sorted(graph_keys - eager_keys)
    divergences: list[dict[str, Any]] = []
    evidence_gaps: list[dict[str, Any]] = []
    for key in sorted(eager_keys & graph_keys):
        for side, record in (("eager", eager[key]), ("graph", graph[key])):
            if not record.get("stats") and not _flatten_numbers(record.get("sample", []), label=f"{side}.sample"):
                evidence_gaps.append({"key": dict(zip(SNAPSHOT_KEY_FIELDS, key)), "side": side,
                                      "reason": "snapshot has no captured numeric values"})
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
        "status": "inconclusive" if evidence_gaps else "exact-match" if not divergences else "diverged",
        "evidence_gaps": evidence_gaps,
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


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphDebugError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise GraphDebugError(f"{label} must be a non-empty object")
    return payload


def snapshot_identity_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_name(snapshot_path.stem + ".identity.json")


def _load_snapshot_observation(
    snapshot_path: Path, *, explicit: Path | None, side: str
) -> dict[str, Any]:
    path = explicit if explicit is not None else snapshot_identity_path(snapshot_path)
    if not path.is_file():
        return {}
    return _load_json_object(path, f"{side} identity")


def build_report(eager_path: Path, graph_path: Path, *, output_dir=None, atol=0.0, rtol=0.0,
                 eager_identity=None, graph_identity=None, allowed_differences=None, workspace_root=ROOT):
    from uuid import uuid4
    from vaws_report import report_directory
    output = report_directory(workspace_root, "vllm-ascend-graph-debug", output_dir)
    if output.exists() and any(output.iterdir()):
        raise GraphDebugError(f"output directory is not empty: {output}")
    run_id = "graph-" + uuid4().hex[:12]
    identities = {
        side: identity_from_mapping(run_id + "-" + side,
                    _load_snapshot_observation(path, explicit=explicit, side=side), origin="observed")
        for side, path, explicit in (("eager", eager_path, eager_identity), ("graph", graph_path, graph_identity))
    }
    certificate = issue_certificate(identities["eager"], identities["graph"],
                                    vary=allowed_differences or ["execution_mode"],
                                    must_observe_prefixes=GRAPH_MUST_OBSERVE)
    comparison = compare_snapshots(eager_path, graph_path, atol=atol, rtol=rtol)
    try:
        consume_certificate(certificate)
    except ComparabilityError as exc:
        comparison["observed_status"] = comparison["status"]
        comparison["status"] = "inconclusive"
        comparison["reason"] = str(exc)
    comparison["comparability"] = certificate
    comparison["claim"] = "Comparison of the supplied snapshots; this does not establish a root cause or a successful model rerun."
    _atomic_write_json(output / "comparison.json", comparison)
    _atomic_write_json(output / "comparability-certificate.json", certificate)
    manifest = new_manifest(run_type="debug", run_id=run_id, workspace_root=workspace_root)
    manifest = transition_status(manifest, "running")
    for name, kind, path in (("eager", "snapshot", eager_path.resolve()), ("graph", "snapshot", graph_path.resolve()),
                             ("comparison", "graph-eager-comparison", Path("comparison.json")),
                             ("comparability-certificate", "comparability-certificate", Path("comparability-certificate.json"))):
        manifest = add_artifact(manifest, name=name, kind=kind, uri=str(path))
    terminal = {"exact-match": "passed", "diverged": "failed", "inconclusive": "inconclusive"}[comparison["status"]]
    manifest = transition_status(manifest, terminal)
    write_manifest(output / "manifest.json", manifest)
    return {"status": comparison["status"], "first_divergence": comparison["first_divergence"],
            "comparison": str(output / "comparison.json"), "manifest_ref": str(output / "manifest.json")}


def build_parser():
    parser = argparse.ArgumentParser(description="Compare collected eager/graph snapshots and their recorded identities.")
    for side in ("eager", "graph"):
        parser.add_argument("--" + side, required=True, type=Path)
        parser.add_argument("--" + side + "-identity", type=Path)
    parser.add_argument("--atol", type=float, default=0.0)
    parser.add_argument("--rtol", type=float, default=0.0)
    parser.add_argument("--allowed-difference", action="append", default=[])
    parser.add_argument("--output-dir", type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = build_report(args.eager, args.graph, output_dir=args.output_dir, atol=args.atol, rtol=args.rtol,
                              eager_identity=args.eager_identity, graph_identity=args.graph_identity,
                              allowed_differences=args.allowed_difference)
    except (GraphDebugError, RunManifestError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
