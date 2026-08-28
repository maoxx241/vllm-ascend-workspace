#!/usr/bin/env python3
"""Control and analyze alternating vLLM Ascend A/B performance experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

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
STATES = ("baseline", "candidate")
PHASES = ("warmup", "measure")
DEFAULT_BENCHMARK_METRICS = {
    "throughput": "output_throughput",
    "ttft": "mean_ttft_ms",
    "tpot": "mean_tpot_ms",
    "itl": "mean_itl_ms",
    "acceptance_rate": "acceptance_rate",
}


class PerformanceRegressionError(ValueError):
    """Raised when experiment parity, schedule, or measurements are invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def emit_progress(phase: str, **details: Any) -> None:
    print(json.dumps({"phase": phase, **details}, ensure_ascii=False), file=sys.stderr)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PerformanceRegressionError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PerformanceRegressionError(f"{label} root must be an object")
    return payload


def config_hash(shared: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        shared, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_metric_maps(values: Sequence[str]) -> dict[str, str]:
    mapping = dict(DEFAULT_BENCHMARK_METRICS)
    for value in values:
        target, separator, source = value.partition("=")
        if not separator or not target.strip() or not source.strip():
            raise PerformanceRegressionError(
                f"metric map must use TARGET=SOURCE, got: {value}"
            )
        mapping[target.strip()] = source.strip()
    return mapping


def normalize_benchmark_result(
    benchmark: Mapping[str, Any],
    *,
    state: str,
    phase: str,
    ordinal: int,
    fingerprint: str,
    source: str,
    metric_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if benchmark.get("status") != "ok":
        raise PerformanceRegressionError("Benchmark result status must be ok")
    raw_metrics = benchmark.get("metrics")
    aggregated = False
    if not isinstance(raw_metrics, Mapping):
        raw_metrics = benchmark.get("aggregated")
        aggregated = True
    if not isinstance(raw_metrics, Mapping):
        raise PerformanceRegressionError(
            "Benchmark result must contain metrics or aggregated"
        )
    normalized_metrics: dict[str, float] = {}
    for target, source_name in (metric_map or DEFAULT_BENCHMARK_METRICS).items():
        raw_value = raw_metrics.get(source_name)
        if aggregated and isinstance(raw_value, Mapping):
            raw_value = raw_value.get("mean")
        if raw_value is None:
            continue
        if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
            raise PerformanceRegressionError(
                f"Benchmark metric {source_name} must be numeric"
            )
        normalized_metrics[target] = float(raw_value)
    measurement = {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "phase": phase,
        "ordinal": ordinal,
        "config_hash": fingerprint,
        "metrics": normalized_metrics,
        "source": source,
    }
    validate_measurement(measurement)
    return measurement


def validate_config(config: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for state in STATES:
        value = config.get(state)
        if not isinstance(value, Mapping):
            errors.append(f"{state} must be an object")
            continue
        for field in ("label", "code_snapshot", "session_id"):
            if not isinstance(value.get(field), str) or not value[field].strip():
                errors.append(f"{state}.{field} must be a non-empty string")
    if isinstance(config.get("baseline"), Mapping) and isinstance(
        config.get("candidate"), Mapping
    ):
        if config["baseline"].get("session_id") == config["candidate"].get("session_id"):
            errors.append("baseline and candidate session_id must be different")
    shared = config.get("shared")
    if not isinstance(shared, Mapping) or not shared:
        errors.append("shared must be a non-empty object")
    runs = config.get("runs")
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < 2:
        errors.append("runs must be an integer of at least 2")
    warmups = config.get("warmups", 1)
    if not isinstance(warmups, int) or isinstance(warmups, bool) or warmups < 1:
        errors.append("warmups must be a positive integer")
    thresholds = config.get("thresholds")
    if not isinstance(thresholds, Mapping) or not thresholds:
        errors.append("thresholds must be a non-empty object")
        thresholds = {}
    for metric, rule in thresholds.items():
        if not isinstance(rule, Mapping):
            errors.append(f"thresholds.{metric} must be an object")
            continue
        if rule.get("direction") not in {"higher", "lower"}:
            errors.append(f"thresholds.{metric}.direction must be higher or lower")
        limit = rule.get("max_relative_regression")
        if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit < 0:
            errors.append(
                f"thresholds.{metric}.max_relative_regression must be non-negative"
            )
    max_cv = config.get("max_cv", 0.1)
    if not isinstance(max_cv, (int, float)) or isinstance(max_cv, bool) or max_cv < 0:
        errors.append("max_cv must be non-negative")
    if errors:
        raise PerformanceRegressionError("; ".join(errors))


def build_schedule(*, warmups: int, runs: int) -> list[dict[str, Any]]:
    schedule: list[dict[str, Any]] = []
    for ordinal in range(1, warmups + 1):
        for state in STATES:
            schedule.append(
                {
                    "index": len(schedule) + 1,
                    "id": f"{state}-warmup-{ordinal}",
                    "state": state,
                    "phase": "warmup",
                    "ordinal": ordinal,
                    "status": "pending",
                }
            )
    for ordinal in range(1, runs + 1):
        order = STATES if ordinal % 2 == 1 else tuple(reversed(STATES))
        for state in order:
            schedule.append(
                {
                    "index": len(schedule) + 1,
                    "id": f"{state}-measure-{ordinal}",
                    "state": state,
                    "phase": "measure",
                    "ordinal": ordinal,
                    "status": "pending",
                }
            )
    return schedule


def plan(
    output_dir: Path,
    *,
    config_path: Path,
    created_at: str | None = None,
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PerformanceRegressionError(f"output directory is not empty: {output_dir}")
    config = _load_json(config_path, "experiment config")
    validate_config(config)
    run_id = config.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise PerformanceRegressionError("run_id must be a non-empty string")
    fingerprint = config_hash(config["shared"])
    schedule = {
        "schema_version": SCHEMA_VERSION,
        "config_hash": fingerprint,
        "entries": build_schedule(warmups=config.get("warmups", 1), runs=config["runs"]),
    }
    timestamp = created_at or utc_now()
    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "planned",
        "config_hash": fingerprint,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    measurements = {
        "schema_version": SCHEMA_VERSION,
        "config_hash": fingerprint,
        "measurements": [],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "experiment-config.json", config)
    _write_json(output_dir / "parity-check.json", {
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "config_hash": fingerprint,
        "shared": config["shared"],
        "allowed_difference": ["code_snapshot", "session_id", "label"],
    })
    _write_json(output_dir / "schedule.json", schedule)
    _write_json(output_dir / "measurements.json", measurements)
    _write_json(output_dir / "run.json", state)
    _atomic_write(
        output_dir / "reproduction.md",
        "# Performance regression reproduction\n\n"
        "Follow `schedule.json` in order. For every entry, establish code parity for "
        "the named state, run the shared Serving and Benchmark configuration, and "
        "record a normalized result with the exact `config_hash`.\n",
    )
    manifest = new_manifest(
        run_type="performance",
        run_id=run_id,
        workspace_snapshot={
            "baseline": config["baseline"]["code_snapshot"],
            "candidate": config["candidate"]["code_snapshot"],
        },
        environment=config["shared"].get("environment", {}),
        model=config["shared"].get("model", {}),
        topology=config["shared"].get("topology", {}),
        created_at=timestamp,
    )
    for name, kind, uri in (
        ("experiment-config", "experiment-config", "experiment-config.json"),
        ("parity-check", "parity-check", "parity-check.json"),
        ("schedule", "schedule", "schedule.json"),
        ("measurements", "measurements", "measurements.json"),
        ("reproduction", "reproduction", "reproduction.md"),
    ):
        manifest = add_artifact(
            manifest, name=name, kind=kind, uri=uri, updated_at=timestamp
        )
    write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": "planned",
        "run_id": run_id,
        "config_hash": fingerprint,
        "schedule_entries": len(schedule["entries"]),
    }


def validate_measurement(measurement: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if measurement.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if measurement.get("state") not in STATES:
        errors.append(f"state must be one of: {', '.join(STATES)}")
    if measurement.get("phase") not in PHASES:
        errors.append(f"phase must be one of: {', '.join(PHASES)}")
    ordinal = measurement.get("ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        errors.append("ordinal must be a positive integer")
    fingerprint = measurement.get("config_hash")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        errors.append("config_hash must be a SHA256 string")
    metrics = measurement.get("metrics")
    if not isinstance(metrics, Mapping) or not metrics:
        errors.append("metrics must be a non-empty object")
    else:
        for name, value in metrics.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"metrics.{name} must be numeric")
            elif not math.isfinite(float(value)):
                errors.append(f"metrics.{name} must be finite")
    if errors:
        raise PerformanceRegressionError("; ".join(errors))


def record(
    output_dir: Path,
    *,
    result_path: Path,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    result = _load_json(result_path, "measurement result")
    validate_measurement(result)
    schedule = _load_json(output_dir / "schedule.json", "schedule")
    measurements = _load_json(output_dir / "measurements.json", "measurements")
    if result["config_hash"] != schedule["config_hash"]:
        raise PerformanceRegressionError(
            "measurement config_hash does not match the planned experiment"
        )
    pending = next(
        (entry for entry in schedule["entries"] if entry["status"] == "pending"),
        None,
    )
    if pending is None:
        raise PerformanceRegressionError("schedule is already complete")
    expected = (pending["state"], pending["phase"], pending["ordinal"])
    actual = (result["state"], result["phase"], result["ordinal"])
    if actual != expected:
        raise PerformanceRegressionError(
            f"out-of-order measurement: expected {expected}, received {actual}"
        )
    timestamp = recorded_at or utc_now()
    normalized = {
        "schedule_id": pending["id"],
        "state": result["state"],
        "phase": result["phase"],
        "ordinal": result["ordinal"],
        "config_hash": result["config_hash"],
        "metrics": {name: float(value) for name, value in result["metrics"].items()},
        "source": result.get("source"),
        "recorded_at": timestamp,
    }
    measurements["measurements"].append(normalized)
    pending["status"] = "recorded"
    _write_json(output_dir / "measurements.json", measurements)
    _write_json(output_dir / "schedule.json", schedule)
    state = _load_json(output_dir / "run.json", "run state")
    if state["status"] == "planned":
        state["status"] = "running"
    state["updated_at"] = timestamp
    _write_json(output_dir / "run.json", state)
    manifest = load_manifest(output_dir / "manifest.json")
    if manifest["status"] == "planned":
        manifest = transition_status(manifest, "running", updated_at=timestamp)
        write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": "recorded",
        "schedule_id": pending["id"],
        "remaining": sum(entry["status"] == "pending" for entry in schedule["entries"]),
    }


def detect_outliers(values: Sequence[float]) -> list[int]:
    if len(values) < 3:
        return []
    median = statistics.median(values)
    deviations = [abs(value - median) for value in values]
    mad = statistics.median(deviations)
    if mad == 0:
        return [index for index, value in enumerate(values) if value != median]
    return [
        index
        for index, value in enumerate(values)
        if 0.6745 * abs(value - median) / mad > 3.5
    ]


def summarize_values(
    values: Sequence[float], *, exclude_outliers: bool
) -> dict[str, Any]:
    outliers = detect_outliers(values)
    decision_values = [
        value for index, value in enumerate(values) if not exclude_outliers or index not in outliers
    ]
    if not decision_values:
        return {
            "values": list(values),
            "outlier_indices": outliers,
            "decision_values": [],
            "count": 0,
            "mean": None,
            "stdev": None,
            "cv": None,
        }
    mean = statistics.fmean(decision_values)
    stdev = statistics.stdev(decision_values) if len(decision_values) > 1 else 0.0
    cv = abs(stdev / mean) if mean != 0 else (math.inf if stdev else 0.0)
    return {
        "values": list(values),
        "outlier_indices": outliers,
        "decision_values": decision_values,
        "count": len(decision_values),
        "mean": mean,
        "stdev": stdev,
        "cv": cv,
    }


def analyze_documents(
    config: Mapping[str, Any],
    schedule: Mapping[str, Any],
    measurements: Mapping[str, Any],
) -> dict[str, Any]:
    validate_config(config)
    pending = [entry["id"] for entry in schedule["entries"] if entry["status"] != "recorded"]
    if pending:
        raise PerformanceRegressionError(
            f"schedule is incomplete; pending entries: {', '.join(pending)}"
        )
    measured = [
        row for row in measurements["measurements"] if row["phase"] == "measure"
    ]
    exclude_outliers = bool(config.get("exclude_outliers", False))
    max_cv = float(config.get("max_cv", 0.1))
    metrics: dict[str, Any] = {}
    missing_metrics: list[str] = []
    noisy_metrics: list[str] = []
    regressions: list[str] = []
    for metric, rule in sorted(config["thresholds"].items()):
        state_values: dict[str, list[float]] = {}
        for state in STATES:
            rows = [row for row in measured if row["state"] == state]
            if any(metric not in row["metrics"] for row in rows):
                missing_metrics.append(metric)
                state_values[state] = []
            else:
                state_values[state] = [float(row["metrics"][metric]) for row in rows]
        baseline_summary = summarize_values(
            state_values["baseline"], exclude_outliers=exclude_outliers
        )
        candidate_summary = summarize_values(
            state_values["candidate"], exclude_outliers=exclude_outliers
        )
        metric_result: dict[str, Any] = {
            "direction": rule["direction"],
            "max_relative_regression": float(rule["max_relative_regression"]),
            "baseline": baseline_summary,
            "candidate": candidate_summary,
            "relative_change": None,
            "regression": None,
        }
        if baseline_summary["count"] < 2 or candidate_summary["count"] < 2:
            if metric not in missing_metrics:
                missing_metrics.append(metric)
        else:
            baseline_mean = baseline_summary["mean"]
            candidate_mean = candidate_summary["mean"]
            relative_change = (
                (candidate_mean - baseline_mean) / abs(baseline_mean)
                if baseline_mean != 0
                else math.inf
                if candidate_mean != 0
                else 0.0
            )
            direction = rule["direction"]
            degradation = -relative_change if direction == "higher" else relative_change
            regression = degradation > float(rule["max_relative_regression"])
            metric_result["relative_change"] = relative_change
            metric_result["regression"] = regression
            if regression:
                regressions.append(metric)
            if baseline_summary["cv"] > max_cv or candidate_summary["cv"] > max_cv:
                noisy_metrics.append(metric)
        metrics[metric] = metric_result
    if missing_metrics or noisy_metrics:
        status = "inconclusive"
    elif regressions:
        status = "failed"
    else:
        status = "passed"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "config_hash": schedule["config_hash"],
        "exclude_outliers": exclude_outliers,
        "max_cv": max_cv,
        "missing_metrics": sorted(set(missing_metrics)),
        "noisy_metrics": sorted(set(noisy_metrics)),
        "regressions": sorted(set(regressions)),
        "metrics": metrics,
    }


def render_report(comparison: Mapping[str, Any]) -> str:
    lines = [
        "# Performance regression report",
        "",
        f"- Status: **{comparison['status']}**",
        f"- Config hash: `{comparison['config_hash']}`",
        f"- Outliers excluded from decision: `{comparison['exclude_outliers']}`",
        f"- Maximum accepted CV: `{comparison['max_cv']}`",
        "",
        "| Metric | Baseline mean | Candidate mean | Relative change | Regression |",
        "|---|---:|---:|---:|---|",
    ]
    for name, row in comparison["metrics"].items():
        baseline = row["baseline"]["mean"]
        candidate = row["candidate"]["mean"]
        change = row["relative_change"]
        lines.append(
            f"| `{name}` | {baseline if baseline is not None else 'N/A'} | "
            f"{candidate if candidate is not None else 'N/A'} | "
            f"{change if change is not None else 'N/A'} | {row['regression']} |"
        )
    lines.extend(["", "## Measurement quality", ""])
    lines.append(
        "- Missing or insufficient metrics: "
        + (", ".join(comparison["missing_metrics"]) or "none")
    )
    lines.append(
        "- Metrics above CV limit: "
        + (", ".join(comparison["noisy_metrics"]) or "none")
    )
    lines.append(
        "- Regressions: " + (", ".join(comparison["regressions"]) or "none")
    )
    lines.extend(["", "## Full statistics", "", "```json"])
    lines.append(json.dumps(comparison["metrics"], ensure_ascii=False, indent=2, sort_keys=True))
    lines.extend(["```", ""])
    return "\n".join(lines)


def analyze(output_dir: Path, *, updated_at: str | None = None) -> dict[str, Any]:
    state = _load_json(output_dir / "run.json", "run state")
    if state["status"] != "running":
        raise PerformanceRegressionError(f"run must be running, got {state['status']}")
    config = _load_json(output_dir / "experiment-config.json", "experiment config")
    schedule = _load_json(output_dir / "schedule.json", "schedule")
    measurements = _load_json(output_dir / "measurements.json", "measurements")
    comparison = analyze_documents(config, schedule, measurements)
    _write_json(output_dir / "comparison.json", comparison)
    _atomic_write(output_dir / "report.md", render_report(comparison))
    timestamp = updated_at or utc_now()
    state["status"] = comparison["status"]
    state["updated_at"] = timestamp
    _write_json(output_dir / "run.json", state)
    manifest = load_manifest(output_dir / "manifest.json")
    for name, kind, uri in (
        ("comparison", "comparison", "comparison.json"),
        ("report", "report", "report.md"),
    ):
        manifest = add_artifact(
            manifest, name=name, kind=kind, uri=uri, updated_at=timestamp
        )
    manifest = transition_status(manifest, comparison["status"], updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": comparison["status"],
        "regressions": comparison["regressions"],
        "noisy_metrics": comparison["noisy_metrics"],
        "comparison": str((output_dir / "comparison.json").resolve()),
        "report": str((output_dir / "report.md").resolve()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan_parser = subparsers.add_parser("plan", help="validate and schedule an experiment")
    plan_parser.add_argument("--output-dir", required=True, type=Path)
    plan_parser.add_argument("--config", required=True, type=Path)
    record_parser = subparsers.add_parser("record", help="record the next measurement")
    record_parser.add_argument("--output-dir", required=True, type=Path)
    record_parser.add_argument("--result", required=True, type=Path)
    normalize_parser = subparsers.add_parser(
        "normalize", help="normalize one Benchmark result"
    )
    normalize_parser.add_argument("--result", required=True, type=Path)
    normalize_parser.add_argument("--output", required=True, type=Path)
    normalize_parser.add_argument("--state", required=True, choices=STATES)
    normalize_parser.add_argument("--phase", required=True, choices=PHASES)
    normalize_parser.add_argument("--ordinal", required=True, type=int)
    normalize_parser.add_argument("--config-hash", required=True)
    normalize_parser.add_argument(
        "--metric-map",
        action="append",
        default=[],
        metavar="TARGET=SOURCE",
        help="add or override a Benchmark-to-measurement metric mapping",
    )
    analyze_parser = subparsers.add_parser("analyze", help="analyze completed measurements")
    analyze_parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "plan":
            emit_progress("plan", output_dir=str(args.output_dir))
            payload = plan(args.output_dir, config_path=args.config)
        elif args.action == "normalize":
            emit_progress("normalize", result=str(args.result))
            benchmark = _load_json(args.result, "Benchmark result")
            normalized = normalize_benchmark_result(
                benchmark,
                state=args.state,
                phase=args.phase,
                ordinal=args.ordinal,
                fingerprint=args.config_hash,
                source=str(args.result.resolve()),
                metric_map=parse_metric_maps(args.metric_map),
            )
            _write_json(args.output, normalized)
            payload = {
                "status": "normalized",
                "output": str(args.output.resolve()),
                "metrics": sorted(normalized["metrics"]),
            }
        elif args.action == "record":
            emit_progress("record", result=str(args.result))
            payload = record(args.output_dir, result_path=args.result)
        else:
            emit_progress("analyze", output_dir=str(args.output_dir))
            payload = analyze(args.output_dir)
    except (PerformanceRegressionError, RunManifestError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
