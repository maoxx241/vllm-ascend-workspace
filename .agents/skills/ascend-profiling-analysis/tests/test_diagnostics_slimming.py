"""Regression tests for diagnostics finding-metrics slimming:

- ``diagnose_cross_rank`` copies only whitelisted keys into finding
  ``metrics`` — the giant ``event_ids`` cell from the alignment row must
  not be replicated into every finding; human-readable scalars
  (durations, skews, rank/step context) stay;
- ``diagnose_profile`` ``counts.by_type`` is Counter-derived and matches
  the actual finding types.
"""

from __future__ import annotations

import json
from pathlib import Path

import conftest  # noqa: F401 — registers sys.path

from ascend_profile import diagnostics
from ascend_profile.common import write_csv


def _cross_rank_row(**overrides):
    row = {
        "alignment_id": "align_abc",
        "alignment_type": "operator",
        "rank_ids": json.dumps(["rank_0", "rank_1"]),
        "segment_ids": json.dumps([]),
        "event_ids": json.dumps([f"evt_{index}" for index in range(64)]),
        "start_us": "1000.0",
        "end_us": "1010.0",
        "alignment_method": "time_bucket_v1",
        "alignment_confidence": "low",
        "alignment_limitations": "bucket caveat",
        "role": "communication.collective",
        "name_key": "hcom_allreduce",
        "shape_signature": "shape_a",
        "bucket_us": "1000.0",
        "member_count": "200",
        "rank_count": "8",
        "start_skew_us": "5.0",
        "duration_min_us": "9.0",
        "duration_max_us": "90.0",
        "duration_skew_us": "81.0",
        "duration_ratio": "10.0",
        "wait_max_us": "3.0",
        "wall_skew_us": "",
        "layer_counts": "",
        "step_families": "",
        "is_structure_mismatch": "False",
        "evidence_ids": json.dumps([]),
    }
    row.update(overrides)
    return row


def test_cross_rank_finding_metrics_drop_event_ids_cell() -> None:
    findings = diagnostics.diagnose_cross_rank([_cross_rank_row()])
    assert len(findings) == 1
    finding = findings[0]
    assert finding.finding_type == "communication_collective_slow"
    assert "event_ids" not in finding.metrics
    # Human-readable scalars and rank/step context survive.
    assert finding.metrics["duration_ratio"] == "10.0"
    assert finding.metrics["duration_skew_us"] == "81.0"
    assert finding.metrics["start_skew_us"] == "5.0"
    assert finding.metrics["role"] == "communication.collective"
    assert finding.metrics["rank_ids"] == '["rank_0", "rank_1"]'
    assert finding.metrics["member_count"] == "200"
    assert finding.metrics["alignment_id"] == "align_abc"


def test_cross_rank_finding_metrics_whitelist_applies_to_all_types() -> None:
    rows = [
        _cross_rank_row(alignment_type="time_window", is_structure_mismatch="True"),
        _cross_rank_row(role="moe.dispatch_expert_compute"),
        _cross_rank_row(role="compute.matmul", start_skew_us="99999.0"),
    ]
    findings = diagnostics.diagnose_cross_rank(rows)
    types = {finding.finding_type for finding in findings}
    assert types == {
        "rank_workload_asymmetry",
        "communication_collective_slow",
        "ep_load_imbalance_suspected",
        "slow_rank_suspected",
    }
    for finding in findings:
        assert "event_ids" not in finding.metrics


def test_diagnose_profile_by_type_counts(tmp_path: Path) -> None:
    rows = [
        _cross_rank_row(alignment_id="align_1"),
        _cross_rank_row(alignment_id="align_2"),
        _cross_rank_row(alignment_id="align_3", role="compute.matmul", start_skew_us="99999.0"),
    ]
    write_csv(tmp_path / "cross_rank_alignment.csv", rows)
    payload = diagnostics.diagnose_profile(tmp_path)
    assert payload["counts"]["finding_count"] == 3
    assert payload["counts"]["by_type"] == {"communication_collective_slow": 2, "slow_rank_suspected": 1}
    written = json.loads((tmp_path / "diagnosis_findings.json").read_text(encoding="utf-8"))
    for finding in written["diagnosis_findings"]:
        assert "event_ids" not in finding["metrics"]


if __name__ == "__main__":
    test_cross_rank_finding_metrics_drop_event_ids_cell()
    print("ok")
