#!/usr/bin/env python3
"""Generate diagnosis claims from summary and cross-rank evidence tables.

Thresholds, severity/confidence labels, message templates, and static
limitations live in ``knowledge/diagnosis_rules.yaml`` (loaded at runtime by
``load_diagnosis_rules``). The *condition logic* — which rows trigger which
finding — stays here in Python on purpose; the YAML carries constants and
wording only, no rule DSL.
"""

from __future__ import annotations

import argparse
import functools
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .common import DiagnosisFinding, SCHEMA_VERSION, TOOL_VERSION, csv_rows, emit_stage_json, stable_id, utc_now, write_json
    from .store import KNOWLEDGE_DIR, parse_jsonish
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import DiagnosisFinding, SCHEMA_VERSION, TOOL_VERSION, csv_rows, emit_stage_json, stable_id, utc_now, write_json  # type: ignore[no-redef]
    from store import KNOWLEDGE_DIR, parse_jsonish  # type: ignore[no-redef]


DIAGNOSIS_RULES_PATH = KNOWLEDGE_DIR / "diagnosis_rules.yaml"

_FINDING_KEYS = frozenset({
    "scope",
    "severity",
    "escalated_severity",
    "confidence",
    "summary",
    "summary_template",
    "limitations",
    "limitations_if_no_evidence",
    "limitation_parts",
})


def load_diagnosis_rules(path: Path = DIAGNOSIS_RULES_PATH) -> dict[str, Any]:
    """Load and validate ``knowledge/diagnosis_rules.yaml``.

    This is the single source of truth for finding thresholds and wording,
    not a silent fallback: if the file is missing or malformed we raise
    instead of guessing.
    """

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - yaml is a hard dependency
        raise RuntimeError("PyYAML is required to load knowledge/diagnosis_rules.yaml") from exc
    if not path.exists():
        raise RuntimeError(f"diagnosis rules knowledge base missing: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    thresholds = doc.get("thresholds")
    if not isinstance(thresholds, dict) or not thresholds:
        raise RuntimeError(f"{path.name}: thresholds: missing or not a mapping")
    for key, value in thresholds.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise RuntimeError(f"{path.name}: thresholds.{key}: must be numeric, got {value!r}")

    findings = doc.get("findings")
    if not isinstance(findings, dict) or not findings:
        raise RuntimeError(f"{path.name}: findings: missing or not a mapping")
    for finding_type, meta in findings.items():
        context = f"{path.name}: findings.{finding_type}"
        if not isinstance(meta, dict):
            raise RuntimeError(f"{context}: must be a mapping")
        unknown = set(meta) - _FINDING_KEYS
        if unknown:
            raise RuntimeError(f"{context}: unknown keys: {sorted(unknown)}")
        for key in ("scope", "severity", "confidence"):
            if not isinstance(meta.get(key), str) or not meta[key]:
                raise RuntimeError(f"{context}: {key} must be a non-empty string")
        if ("summary" in meta) == ("summary_template" in meta):
            raise RuntimeError(f"{context}: exactly one of summary / summary_template is required")
        if "escalated_severity" in meta and not isinstance(meta["escalated_severity"], str):
            raise RuntimeError(f"{context}: escalated_severity must be a string")
        for key in ("limitations", "limitations_if_no_evidence"):
            value = meta.get(key)
            if value is not None and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
                raise RuntimeError(f"{context}: {key} must be a list of strings")
        parts = meta.get("limitation_parts")
        if parts is not None and (not isinstance(parts, dict) or not all(isinstance(v, str) for v in parts.values())):
            raise RuntimeError(f"{context}: limitation_parts must be a mapping of strings")
    return {"thresholds": dict(thresholds), "findings": dict(findings)}


@functools.lru_cache(maxsize=1)
def _rules() -> dict[str, Any]:
    return load_diagnosis_rules()


def _thresholds() -> dict[str, float]:
    return _rules()["thresholds"]


def _meta(finding_type: str) -> Mapping[str, Any]:
    meta = _rules()["findings"].get(finding_type)
    if meta is None:  # pragma: no cover - guards Python/YAML drift
        raise RuntimeError(f"diagnosis_rules.yaml: no findings.{finding_type} entry")
    return meta


def as_float(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def finding(
    *,
    finding_type: str,
    scope: str,
    summary: str,
    severity: str,
    confidence: str,
    rank_ids: Sequence[str] = (),
    alignment_ids: Sequence[str] = (),
    evidence_ids: Sequence[str] = (),
    metrics: Mapping[str, Any] | None = None,
    limitations: Sequence[str] = (),
) -> DiagnosisFinding:
    claim_id = stable_id("claim", finding_type, scope, summary, rank_ids, alignment_ids)
    return DiagnosisFinding(
        claim_id=claim_id,
        claim_type=finding_type,
        finding_type=finding_type,
        scope=scope,
        summary=summary,
        severity=severity,
        confidence=confidence,
        rank_ids=tuple(rank_ids),
        alignment_ids=tuple(alignment_ids),
        evidence_ids=tuple(evidence_ids),
        limitations=tuple(limitations),
        metrics=dict(metrics or {}),
    )


def _skew_severity(meta: Mapping[str, Any], duration_ratio: float) -> str:
    if duration_ratio >= _thresholds()["high_skew_ratio"]:
        return str(meta.get("escalated_severity") or meta["severity"])
    return str(meta["severity"])


def diagnose_cross_rank(alignment_rows: Sequence[Mapping[str, Any]]) -> list[DiagnosisFinding]:
    thresholds = _thresholds()
    findings: list[DiagnosisFinding] = []
    for row in alignment_rows:
        alignment_id = str(row.get("alignment_id") or "")
        alignment_type = str(row.get("alignment_type") or "")
        rank_ids = parse_jsonish(row.get("rank_ids"), [])
        role = str(row.get("role") or "")
        duration_ratio = as_float(row, "duration_ratio", 1.0)
        duration_skew = as_float(row, "duration_skew_us")
        start_skew = as_float(row, "start_skew_us")
        is_structure_mismatch = str(row.get("is_structure_mismatch")).lower() == "true"
        if alignment_type == "time_window" and is_structure_mismatch:
            meta = _meta("rank_workload_asymmetry")
            findings.append(
                finding(
                    finding_type="rank_workload_asymmetry",
                    scope=str(meta["scope"]),
                    summary=str(meta["summary"]),
                    severity=str(meta["severity"]),
                    confidence=str(meta["confidence"]),
                    rank_ids=rank_ids,
                    alignment_ids=(alignment_id,),
                    metrics=dict(row),
                )
            )
        if role == "communication.collective" and (duration_ratio >= thresholds["cross_rank_skew_ratio"] or duration_skew >= thresholds["cross_rank_skew_us"]):
            meta = _meta("communication_collective_slow")
            findings.append(
                finding(
                    finding_type="communication_collective_slow",
                    scope=str(meta["scope"]),
                    summary=str(meta["summary"]),
                    severity=_skew_severity(meta, duration_ratio),
                    confidence=str(meta["confidence"]),
                    rank_ids=rank_ids,
                    alignment_ids=(alignment_id,),
                    metrics=dict(row),
                )
            )
        if role in {"moe.dispatch_expert_compute", "moe.dispatch_or_combine"} and (
            duration_ratio >= thresholds["cross_rank_skew_ratio"] or duration_skew >= thresholds["cross_rank_skew_us"]
        ):
            meta = _meta("ep_load_imbalance_suspected")
            findings.append(
                finding(
                    finding_type="ep_load_imbalance_suspected",
                    scope=str(meta["scope"]),
                    summary=str(meta["summary"]),
                    severity=_skew_severity(meta, duration_ratio),
                    confidence=str(meta["confidence"]),
                    rank_ids=rank_ids,
                    alignment_ids=(alignment_id,),
                    metrics=dict(row),
                )
            )
        if role == "compute.matmul" and start_skew >= thresholds["cross_rank_skew_us"]:
            meta = _meta("slow_rank_suspected")
            findings.append(
                finding(
                    finding_type="slow_rank_suspected",
                    scope=str(meta["scope"]),
                    summary=str(meta["summary"]),
                    severity=str(meta["severity"]),
                    confidence=str(meta["confidence"]),
                    rank_ids=rank_ids,
                    alignment_ids=(alignment_id,),
                    metrics=dict(row),
                )
            )
    return findings


def diagnose_rank_workload(rank_rows: Sequence[Mapping[str, Any]], step_rows: Sequence[Mapping[str, Any]]) -> list[DiagnosisFinding]:
    findings: list[DiagnosisFinding] = []
    attention_by_rank = {str(row.get("rank_id")): str(row.get("has_attention")).lower() == "true" for row in rank_rows}
    if attention_by_rank and any(attention_by_rank.values()) and not all(attention_by_rank.values()):
        reduced = [rank for rank, has_attention in attention_by_rank.items() if not has_attention]
        full = [rank for rank, has_attention in attention_by_rank.items() if has_attention]
        meta = _meta("reduced_work_or_dummy_rank")
        findings.append(
            finding(
                finding_type="reduced_work_or_dummy_rank",
                scope=str(meta["scope"]),
                summary=str(meta["summary"]),
                severity=str(meta["severity"]),
                confidence=str(meta["confidence"]),
                rank_ids=tuple(sorted(reduced + full)),
                metrics={"full_work_ranks": full, "reduced_work_candidate_ranks": reduced},
                limitations=tuple(meta.get("limitations") or ()),
            )
        )
    wall_by_rank = {str(row.get("rank_id")): as_float(row, "wall_ms") for row in rank_rows}
    if wall_by_rank:
        values = [value for value in wall_by_rank.values() if value > 0]
        if values and max(values) / max(1e-6, min(values)) >= _thresholds()["dp_wall_skew_ratio"]:
            meta = _meta("dp_workload_imbalance")
            findings.append(
                finding(
                    finding_type="dp_workload_imbalance",
                    scope=str(meta["scope"]),
                    summary=str(meta["summary"]),
                    severity=str(meta["severity"]),
                    confidence=str(meta["confidence"]),
                    rank_ids=tuple(sorted(wall_by_rank)),
                    metrics={"rank_wall_ms": wall_by_rank, "wall_ratio": max(values) / max(1e-6, min(values))},
                    limitations=tuple(meta.get("limitations") or ()),
                )
            )
    for row in step_rows:
        tags = parse_jsonish(row.get("anomaly_tags"), [])
        if "DEVICE_IDLE_GAP_HEAVY" in tags or "INTERNAL_BUBBLE_HEAVY" in tags:
            meta = _meta("device_idle_bubble")
            findings.append(
                finding(
                    finding_type="device_idle_bubble",
                    scope=str(meta["scope"]),
                    summary=str(meta["summary_template"]).format(segment_id=row.get("segment_id")),
                    severity=str(meta["severity"]),
                    confidence=str(meta["confidence"]),
                    rank_ids=(str(row.get("rank_id")),),
                    evidence_ids=tuple(parse_jsonish(row.get("evidence_ids"), [])),
                    metrics=dict(row),
                )
            )
    return findings


def diagnose_recurring_bubbles(rank_rows: Sequence[Mapping[str, Any]], step_rows: Sequence[Mapping[str, Any]]) -> list[DiagnosisFinding]:
    """Rank-level recurring-bubble finding (rulebook §10 of the retired
    ascend-profiling-anomaly skill: >= 60% of complete steps with
    ``bubble_count > 0``).

    The flag and the dominant idle family are computed in
    ``summarize.recurring_bubble_rollup`` and read back from
    ``rank_summary.csv``; per-step evidence ids come from the bubbling
    steps in ``step_summary.csv`` (capped at
    ``thresholds.recurring_bubble_evidence_cap`` so the finding stays
    readable).
    """

    evidence_cap = int(_thresholds()["recurring_bubble_evidence_cap"])
    meta = _meta("recurring_bubble_pattern")
    bubbling_evidence_by_rank: dict[str, list[str]] = defaultdict(list)
    for row in step_rows:
        if row.get("segment_type") != "step":
            continue
        if as_float(row, "bubble_count") <= 0:
            continue
        rank_id = str(row.get("rank_id") or "")
        evidence_ids = parse_jsonish(row.get("evidence_ids"), [])
        if evidence_ids:
            bubbling_evidence_by_rank[rank_id].append(str(evidence_ids[0]))
    findings: list[DiagnosisFinding] = []
    for row in rank_rows:
        if str(row.get("recurring_bubble_pattern")).lower() != "true":
            continue
        rank_id = str(row.get("rank_id") or "")
        evidence = tuple(bubbling_evidence_by_rank.get(rank_id, [])[:evidence_cap])
        limitations: tuple[str, ...] = ()
        if not evidence:
            limitations = tuple(meta.get("limitations_if_no_evidence") or ())
        findings.append(
            finding(
                finding_type="recurring_bubble_pattern",
                scope=str(meta["scope"]),
                summary=str(meta["summary_template"]).format(
                    rank_id=rank_id,
                    recurrence_pct=as_float(row, "bubble_recurrence_ratio") * 100,
                    dominant_idle_pattern=row.get("dominant_idle_pattern"),
                ),
                severity=str(meta["severity"]),
                confidence=str(meta["confidence"]),
                rank_ids=(rank_id,),
                evidence_ids=evidence,
                metrics={
                    "bubble_recurrence_ratio": as_float(row, "bubble_recurrence_ratio"),
                    "bubbling_step_count": row.get("bubbling_step_count"),
                    "dominant_idle_pattern": row.get("dominant_idle_pattern"),
                    "step_count": row.get("step_count"),
                },
                limitations=limitations,
            )
        )
    return findings


def diagnose_profile(output_dir: Path) -> dict[str, Any]:
    alignment_rows = csv_rows(output_dir / "cross_rank_alignment.csv")
    rank_rows = csv_rows(output_dir / "rank_summary.csv")
    step_rows = csv_rows(output_dir / "step_summary.csv")
    wait_rows = csv_rows(output_dir / "wait_anchor_ops.csv")
    aicpu_rows = csv_rows(output_dir / "aicpu_summary.csv")
    findings = diagnose_cross_rank(alignment_rows)
    findings.extend(diagnose_rank_workload(rank_rows, step_rows))
    findings.extend(diagnose_recurring_bubbles(rank_rows, step_rows))
    for row in wait_rows:
        if str(row.get("is_false_hotspot_risk")).lower() == "true":
            meta = _meta("wait_anchor_false_hotspot")
            parts = meta["limitation_parts"]
            row_ranges = str(row.get("row_ranges") or "").strip()
            sample_evt = str(row.get("sample_event_ids") or "").strip()
            limitation_parts = [str(parts["base"])]
            if row_ranges:
                limitation_parts.append(str(parts["row_ranges"]).format(row_ranges=row_ranges))
            if sample_evt:
                limitation_parts.append(str(parts["sample_event_ids"]).format(sample_event_ids=sample_evt))
            findings.append(
                finding(
                    finding_type="wait_anchor_false_hotspot",
                    scope=str(meta["scope"]),
                    summary=str(meta["summary_template"]).format(name=row.get("name")),
                    severity=str(meta["severity"]),
                    confidence=str(meta["confidence"]),
                    rank_ids=(str(row.get("rank_id")),),
                    metrics=dict(row),
                    limitations=(" ".join(limitation_parts),),
                )
            )
    for row in aicpu_rows:
        if str(row.get("classification")) == "AICPU_EXPOSED_NOT_ALLOWED":
            meta = _meta("aicpu_exposed")
            findings.append(
                finding(
                    finding_type="aicpu_exposed",
                    scope=str(meta["scope"]),
                    summary=str(meta["summary_template"]).format(name=row.get("name")),
                    severity=str(meta["severity"]),
                    confidence=str(meta["confidence"]),
                    rank_ids=(str(row.get("rank_id")),),
                    metrics=dict(row),
                    limitations=tuple(meta.get("limitations") or ()),
                )
            )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "analysis_stage": "diagnostics",
        "created_at": utc_now(),
        "diagnosis_findings": findings,
        "counts": {
            "finding_count": len(findings),
            "by_type": dict(sorted({item.finding_type: sum(1 for finding_item in findings if finding_item.finding_type == item.finding_type) for item in findings}.items())),
        },
    }
    write_json(output_dir / "diagnosis_findings.json", payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = diagnose_profile(Path(args.output))
    emit_stage_json({"stage": "diagnostics", "counts": payload["counts"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
