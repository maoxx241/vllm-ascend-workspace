#!/usr/bin/env python3
"""Generate diagnosis claims from summary and cross-rank evidence tables."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .common import DiagnosisFinding, SCHEMA_VERSION, TOOL_VERSION, csv_rows, stable_id, utc_now, write_json
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import DiagnosisFinding, SCHEMA_VERSION, TOOL_VERSION, csv_rows, stable_id, utc_now, write_json  # type: ignore[no-redef]


CROSS_RANK_SKEW_RATIO = 2.0
HIGH_SKEW_RATIO = 4.0
CROSS_RANK_SKEW_US = 1000.0
DP_WALL_SKEW_RATIO = 2.0


def parse_jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


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


def diagnose_cross_rank(alignment_rows: Sequence[Mapping[str, Any]]) -> list[DiagnosisFinding]:
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
            findings.append(
                finding(
                    finding_type="rank_workload_asymmetry",
                    scope="cross_rank",
                    summary="Aligned ranks show different step family or layer-count structure in the same time window.",
                    severity="medium",
                    confidence="medium",
                    rank_ids=rank_ids,
                    alignment_ids=(alignment_id,),
                    metrics=dict(row),
                )
            )
        if role == "communication.collective" and (duration_ratio >= CROSS_RANK_SKEW_RATIO or duration_skew >= CROSS_RANK_SKEW_US):
            findings.append(
                finding(
                    finding_type="communication_collective_slow",
                    scope="cross_rank",
                    summary="Aligned collective communication shows large duration skew across ranks.",
                    severity="high" if duration_ratio >= HIGH_SKEW_RATIO else "medium",
                    confidence="medium",
                    rank_ids=rank_ids,
                    alignment_ids=(alignment_id,),
                    metrics=dict(row),
                )
            )
        if role in {"moe.dispatch_expert_compute", "moe.dispatch_or_combine"} and (
            duration_ratio >= CROSS_RANK_SKEW_RATIO or duration_skew >= CROSS_RANK_SKEW_US
        ):
            findings.append(
                finding(
                    finding_type="ep_load_imbalance_suspected",
                    scope="cross_rank",
                    summary="Aligned MoE dispatch/combine work shows large duration skew across ranks.",
                    severity="high" if duration_ratio >= HIGH_SKEW_RATIO else "medium",
                    confidence="medium",
                    rank_ids=rank_ids,
                    alignment_ids=(alignment_id,),
                    metrics=dict(row),
                )
            )
        if role == "compute.matmul" and start_skew >= CROSS_RANK_SKEW_US:
            findings.append(
                finding(
                    finding_type="slow_rank_suspected",
                    scope="cross_rank",
                    summary="Aligned matmul work has similar operator/shape signature but large launch-time skew.",
                    severity="medium",
                    confidence="medium",
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
        findings.append(
            finding(
                finding_type="reduced_work_or_dummy_rank",
                scope="cross_rank",
                summary="Some ranks lack attention/body evidence while other ranks contain full attention workload evidence.",
                severity="medium",
                confidence="medium",
                rank_ids=tuple(sorted(reduced + full)),
                metrics={"full_work_ranks": full, "reduced_work_candidate_ranks": reduced},
                limitations=("This is structural evidence only; semantic dummy-run labeling requires workload context.",),
            )
        )
    wall_by_rank = {str(row.get("rank_id")): as_float(row, "wall_ms") for row in rank_rows}
    if wall_by_rank:
        values = [value for value in wall_by_rank.values() if value > 0]
        if values and max(values) / max(1e-6, min(values)) >= DP_WALL_SKEW_RATIO:
            findings.append(
                finding(
                    finding_type="dp_workload_imbalance",
                    scope="cross_rank",
                    summary="Rank-level capture wall time differs significantly; check DP workload and T-axis/shape distribution.",
                    severity="medium",
                    confidence="low",
                    rank_ids=tuple(sorted(wall_by_rank)),
                    metrics={"rank_wall_ms": wall_by_rank, "wall_ratio": max(values) / max(1e-6, min(values))},
                    limitations=("Wall-time skew alone is not root cause evidence; shape-level corroboration is required.",),
                )
            )
    for row in step_rows:
        tags = parse_jsonish(row.get("anomaly_tags"), [])
        if "DEVICE_IDLE_GAP_HEAVY" in tags or "INTERNAL_BUBBLE_HEAVY" in tags:
            findings.append(
                finding(
                    finding_type="device_idle_bubble",
                    scope="step",
                    summary=f"Step {row.get('segment_id')} has heavy device idle bubbles.",
                    severity="medium",
                    confidence="high",
                    rank_ids=(str(row.get("rank_id")),),
                    evidence_ids=tuple(parse_jsonish(row.get("evidence_ids"), [])),
                    metrics=dict(row),
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
    for row in wait_rows:
        if str(row.get("is_false_hotspot_risk")).lower() == "true":
            findings.append(
                finding(
                    finding_type="wait_anchor_false_hotspot",
                    scope="operator",
                    summary=f"Operator {row.get('name')} has high wait ratio and low execution duration.",
                    severity="low",
                    confidence="high",
                    rank_ids=(str(row.get("rank_id")),),
                    metrics=dict(row),
                )
            )
    for row in aicpu_rows:
        if str(row.get("classification")) == "AICPU_EXPOSED_NOT_ALLOWED":
            findings.append(
                finding(
                    finding_type="aicpu_exposed",
                    scope="operator",
                    summary=f"AICPU operator {row.get('name')} appears exposed rather than hidden by AI Core work.",
                    severity="medium",
                    confidence="medium",
                    rank_ids=(str(row.get("rank_id")),),
                    metrics=dict(row),
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = diagnose_profile(Path(args.output))
    print({"stage": "diagnostics", "counts": payload["counts"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
