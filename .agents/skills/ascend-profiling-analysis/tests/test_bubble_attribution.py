"""Tests for the idle-pattern detection salvaged from the retired
user-level ``ascend-profiling-anomaly`` skill:

- ``summarize.anomaly_tags`` edge-gap tags (``PRELAUNCH_GAP_HEAVY`` /
  ``TAIL_GAP_HEAVY``), thresholds from the old rulebook §10;
- ``summarize.neighbor_gap_map`` inter-segment idle attribution;
- ``summarize.recurring_bubble_rollup`` >=60% recurrence + dominant idle
  pattern, and the matching ``diagnostics`` finding;
- ``summarize.apply_partial_capture_boundary_tags`` conservatism;
- ``host_trace`` streaming parse + rulebook §11 soft attribution,
  including graceful degradation when ``trace_view.json`` is missing.

All fixtures are tiny synthetic structures; no NPU / remote access.
"""

from __future__ import annotations

import json

import conftest  # noqa: F401

from ascend_profile import diagnostics, host_trace, summarize
from ascend_profile.common import StepSegment


def _segment(
    segment_id: str,
    *,
    rank_id: str = "rank_0",
    segment_type: str = "step",
    complete: bool = True,
    row_start: int = 0,
    row_end: int = 0,
    start_us: float = 0.0,
    end_us: float = 0.0,
) -> StepSegment:
    return StepSegment(
        segment_id=segment_id,
        rank_id=rank_id,
        segment_type=segment_type,
        complete=complete,
        row_start=row_start,
        row_end=row_end,
        start_us=start_us,
        end_us=end_us,
    )


def _base_metrics(**overrides):
    metrics = {
        "wall_ms": 20.0,
        "underfeed_ratio": 0.0,
        "largest_internal_bubble_ms": 0.0,
        "prelaunch_gap_ms": None,
        "tail_gap_ms": None,
    }
    metrics.update(overrides)
    return metrics


# ---------------------------------------------------------------------------
# Edge-gap anomaly tags (rulebook §10: gap >= max(1.0 ms, 10% of wall))
# ---------------------------------------------------------------------------


def test_prelaunch_gap_tag_triggers_above_wall_ratio() -> None:
    tags = summarize.anomaly_tags(_base_metrics(prelaunch_gap_ms=2.5))
    assert "PRELAUNCH_GAP_HEAVY" in tags


def test_prelaunch_gap_below_wall_ratio_no_tag() -> None:
    # wall 20 ms -> threshold max(1.0, 2.0) = 2.0 ms; 1.5 ms stays quiet.
    tags = summarize.anomaly_tags(_base_metrics(prelaunch_gap_ms=1.5))
    assert "PRELAUNCH_GAP_HEAVY" not in tags


def test_edge_gap_absolute_floor() -> None:
    # wall 2 ms -> threshold max(1.0, 0.2) = 1.0 ms.
    assert "TAIL_GAP_HEAVY" not in summarize.anomaly_tags(_base_metrics(wall_ms=2.0, tail_gap_ms=0.5))
    assert "TAIL_GAP_HEAVY" in summarize.anomaly_tags(_base_metrics(wall_ms=2.0, tail_gap_ms=1.0))


def test_tail_gap_tag_triggers() -> None:
    tags = summarize.anomaly_tags(_base_metrics(tail_gap_ms=3.0))
    assert "TAIL_GAP_HEAVY" in tags
    assert "PRELAUNCH_GAP_HEAVY" not in tags


def test_edge_gap_none_means_unknown_never_tags() -> None:
    """No neighbour segment (capture edge) => gap unknown, never tagged."""
    tags = summarize.anomaly_tags(_base_metrics(prelaunch_gap_ms=None, tail_gap_ms=None))
    assert "PRELAUNCH_GAP_HEAVY" not in tags
    assert "TAIL_GAP_HEAVY" not in tags


def test_existing_tags_unchanged() -> None:
    tags = summarize.anomaly_tags(_base_metrics(underfeed_ratio=0.5, largest_internal_bubble_ms=5.0))
    assert tags == ["DEVICE_IDLE_GAP_HEAVY", "INTERNAL_BUBBLE_HEAVY"]


# ---------------------------------------------------------------------------
# neighbor_gap_map
# ---------------------------------------------------------------------------


def test_neighbor_gap_map_attributes_inter_segment_idle() -> None:
    segments = [
        _segment("seg_a", row_start=0, row_end=9, start_us=0.0, end_us=100.0),
        _segment("seg_b", row_start=10, row_end=19, start_us=150.0, end_us=300.0),
        _segment("seg_c", row_start=20, row_end=29, start_us=400.0, end_us=500.0),
    ]
    gaps = summarize.neighbor_gap_map(segments)
    assert gaps["seg_a"]["prelaunch_gap_ms"] is None
    assert gaps["seg_a"]["tail_gap_ms"] == 0.05
    assert gaps["seg_b"]["prelaunch_gap_ms"] == 0.05
    assert gaps["seg_b"]["tail_gap_ms"] == 0.1
    assert gaps["seg_c"]["prelaunch_gap_ms"] == 0.1
    assert gaps["seg_c"]["tail_gap_ms"] is None


def test_neighbor_gap_map_clamps_overlap_to_zero() -> None:
    segments = [
        _segment("seg_a", row_start=0, row_end=9, start_us=0.0, end_us=200.0),
        _segment("seg_b", row_start=10, row_end=19, start_us=150.0, end_us=300.0),
    ]
    gaps = summarize.neighbor_gap_map(segments)
    assert gaps["seg_a"]["tail_gap_ms"] == 0.0
    assert gaps["seg_b"]["prelaunch_gap_ms"] == 0.0


# ---------------------------------------------------------------------------
# recurring_bubble_rollup (rulebook §10: >= 60% of steps bubble)
# ---------------------------------------------------------------------------


def _step_row(segment_id: str, bubble_count: float, **overrides):
    row = {
        "segment_id": segment_id,
        "rank_id": "rank_0",
        "segment_type": "step",
        "bubble_count": bubble_count,
        "prelaunch_gap_ms": 0.0,
        "tail_gap_ms": 0.0,
        "internal_bubble_total_ms": 0.0,
    }
    row.update(overrides)
    return row


def test_recurring_rollup_triggers_at_sixty_percent() -> None:
    rows = [_step_row(f"s{i}", 1.0 if i < 3 else 0.0) for i in range(5)]
    rollup = summarize.recurring_bubble_rollup(rows)
    assert rollup["rank_0"]["bubble_recurrence_ratio"] == 0.6
    assert rollup["rank_0"]["recurring_bubble_pattern"] is True


def test_recurring_rollup_below_threshold_stays_quiet() -> None:
    rows = [_step_row(f"s{i}", 1.0 if i < 2 else 0.0) for i in range(5)]
    rollup = summarize.recurring_bubble_rollup(rows)
    assert rollup["rank_0"]["bubble_recurrence_ratio"] == 0.4
    assert rollup["rank_0"]["recurring_bubble_pattern"] is False


def test_recurring_rollup_min_steps_guard() -> None:
    """2/2 bubbling steps is 100% but too few votes to call a pattern."""
    rows = [_step_row(f"s{i}", 1.0) for i in range(2)]
    rollup = summarize.recurring_bubble_rollup(rows)
    assert rollup["rank_0"]["bubble_recurrence_ratio"] == 1.0
    assert rollup["rank_0"]["recurring_bubble_pattern"] is False


def test_recurring_rollup_dominant_idle_pattern() -> None:
    rows = [
        _step_row("s0", 1.0, prelaunch_gap_ms=4.0, tail_gap_ms=1.0, internal_bubble_total_ms=2.0),
        _step_row("s1", 1.0, prelaunch_gap_ms=2.0, tail_gap_ms=0.0, internal_bubble_total_ms=1.0),
        _step_row("s2", 0.0, prelaunch_gap_ms=3.0, tail_gap_ms=0.5, internal_bubble_total_ms=0.0),
    ]
    rollup = summarize.recurring_bubble_rollup(rows)
    assert rollup["rank_0"]["dominant_idle_pattern"] == "prelaunch"
    silent = [_step_row(f"s{i}", 0.0) for i in range(3)]
    assert summarize.recurring_bubble_rollup(silent)["rank_0"]["dominant_idle_pattern"] == "none"


def test_recurring_rollup_skips_non_step_segments() -> None:
    rows = [
        _step_row("head", 5.0, segment_type="head"),
        *[_step_row(f"s{i}", 0.0) for i in range(3)],
    ]
    rollup = summarize.recurring_bubble_rollup(rows)
    assert rollup["rank_0"]["bubble_recurrence_ratio"] == 0.0
    assert rollup["rank_0"]["recurring_bubble_pattern"] is False


def test_recurring_bubble_finding_from_rank_rows() -> None:
    rank_rows = [
        {
            "rank_id": "rank_0",
            "recurring_bubble_pattern": "True",
            "bubble_recurrence_ratio": "0.6",
            "bubbling_step_count": "3",
            "dominant_idle_pattern": "internal_bubble",
            "step_count": "5",
        },
        {"rank_id": "rank_1", "recurring_bubble_pattern": "False"},
    ]
    step_rows = [
        {
            "segment_id": f"s{i}",
            "rank_id": "rank_0",
            "segment_type": "step",
            "bubble_count": "1.0" if i < 3 else "0.0",
            "evidence_ids": json.dumps([f"evd_{i}"]),
        }
        for i in range(5)
    ]
    findings = diagnostics.diagnose_recurring_bubbles(rank_rows, step_rows)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.finding_type == "recurring_bubble_pattern"
    assert finding.rank_ids == ("rank_0",)
    assert finding.evidence_ids == ("evd_0", "evd_1", "evd_2")
    assert finding.metrics["dominant_idle_pattern"] == "internal_bubble"


def test_recurring_bubble_finding_falls_back_to_limitation_without_evidence() -> None:
    rank_rows = [{"rank_id": "rank_0", "recurring_bubble_pattern": "true", "bubble_recurrence_ratio": "1.0"}]
    findings = diagnostics.diagnose_recurring_bubbles(rank_rows, [])
    assert len(findings) == 1
    assert findings[0].limitations, "no step evidence -> explicit limitation keeps the evidence chain valid"


# ---------------------------------------------------------------------------
# apply_partial_capture_boundary_tags (conservative)
# ---------------------------------------------------------------------------


def _capture_row(
    segment_id: str,
    *,
    complete: bool,
    row_start: int,
    row_end: int,
    event_count: int,
    segment_type: str = "step",
) -> dict:
    return {
        "segment_id": segment_id,
        "rank_id": "rank_0",
        "segment_type": segment_type,
        "complete": complete,
        "row_start": row_start,
        "row_end": row_end,
        "event_count": event_count,
        "anomaly_tags": [],
    }


def _complete_steps(count: int = 3, event_count: int = 100) -> list[dict]:
    return [
        _capture_row(
            f"step_{i}",
            complete=True,
            row_start=i * 100,
            row_end=i * 100 + 99,
            event_count=event_count,
        )
        for i in range(count)
    ]


def test_partial_capture_tags_truncated_tail() -> None:
    rows = _complete_steps()
    rows.append(_capture_row("tail", complete=False, segment_type="tail", row_start=300, row_end=399, event_count=60))
    tagged = summarize.apply_partial_capture_boundary_tags(rows)
    assert tagged == ["tail"]
    assert rows[-1]["anomaly_tags"] == ["PARTIAL_CAPTURE_BOUNDARY"]
    for row in rows[:-1]:
        assert row["anomaly_tags"] == []


def test_partial_capture_ignores_tiny_sliver() -> None:
    rows = _complete_steps()
    rows.append(_capture_row("tail", complete=False, segment_type="tail", row_start=300, row_end=309, event_count=5))
    assert summarize.apply_partial_capture_boundary_tags(rows) == []


def test_partial_capture_ignores_interior_residual() -> None:
    rows = [
        _capture_row("step_0", complete=True, row_start=0, row_end=99, event_count=100),
        _capture_row("residual", complete=False, segment_type="partial_body_window", row_start=100, row_end=199, event_count=60),
        _capture_row("step_1", complete=True, row_start=200, row_end=299, event_count=100),
    ]
    assert summarize.apply_partial_capture_boundary_tags(rows) == []


def test_partial_capture_requires_complete_reference() -> None:
    rows = [
        _capture_row("island", complete=False, segment_type="unclassified_island", row_start=0, row_end=999, event_count=500),
    ]
    assert summarize.apply_partial_capture_boundary_tags(rows) == []


# ---------------------------------------------------------------------------
# host_trace: streaming parse + rulebook §11 soft attribution
# ---------------------------------------------------------------------------


def _write_trace(path, events) -> None:
    path.write_text(json.dumps({"traceEvents": events, "deviceProperties": [{"id": 0}]}), encoding="utf-8")


def test_streaming_parse_filters_host_categories(tmp_path) -> None:
    trace = tmp_path / "trace_view.json"
    _write_trace(
        trace,
        [
            {"ph": "X", "ts": 1000.0, "dur": 500.0, "name": "MatMulV2", "cat": "kernel", "pid": 0, "tid": 2},
            # Braces inside a string arg must not confuse the brace matcher.
            {
                "ph": "X",
                "ts": 1100.0,
                "dur": 600.0,
                "name": "aclrtSynchronizeStream",
                "cat": "AscendCL",
                "pid": 123,
                "tid": 7,
                "args": {"Call Stack": "a.py:1 -> {weird}"},
            },
            {"ph": "X", "ts": 1200.0, "dur": 100.0, "name": "aten::linear", "cat": "cpu_op", "pid": 123, "tid": 1},
            {"ph": "X", "ts": 1300.0, "dur": 100.0, "name": "train.py:step", "cat": "python_function", "pid": 123, "tid": 1},
            {"ph": "i", "name": "ProfilerStep#1", "cat": "user_annotation", "pid": 123, "tid": 1, "ts": 1000.0},
            {"ph": "M", "name": "process_name", "pid": 123, "tid": 0, "args": {"name": "python"}},
        ],
    )
    events, stats = host_trace.collect_host_events(trace, [(900.0, 2000.0)])
    assert [event.name for event in events] == ["aclrtSynchronizeStream", "aten::linear", "train.py:step"]
    assert stats["retained"] == 3 and not stats["truncated"]


def test_collect_host_events_respects_window_filter(tmp_path) -> None:
    trace = tmp_path / "trace_view.json"
    _write_trace(
        trace,
        [
            {"ph": "X", "ts": 100.0, "dur": 50.0, "name": "aten::zero_", "cat": "cpu_op", "pid": 1, "tid": 1},
            {"ph": "X", "ts": 9000.0, "dur": 50.0, "name": "aten::relu", "cat": "cpu_op", "pid": 1, "tid": 1},
        ],
    )
    events, _ = host_trace.collect_host_events(trace, [(1000.0, 2000.0)])
    assert events == []


def test_soft_attribution_sync_marker_label() -> None:
    bubble = (1000.0, 2000.0)
    events = [host_trace.HostEvent(name="aclrtSynchronizeStream", cat="ascendcl", ts_us=1000.0, dur_us=600.0, tid="7")]
    result = host_trace.soft_attribution_for_window(*bubble, events)
    assert "possible_sync_or_h2d" in result["soft_root_cause_labels"]
    assert result["sync_marker_overlap_ratio"] == 0.6


def test_soft_attribution_comm_marker_label() -> None:
    bubble = (1000.0, 2000.0)
    events = [host_trace.HostEvent(name="c10d::allreduce_", cat="cpu_op", ts_us=1100.0, dur_us=400.0, tid="1")]
    result = host_trace.soft_attribution_for_window(*bubble, events)
    assert result["soft_root_cause_labels"] == ["possible_comm_wait"]


def test_soft_attribution_untraced_host_blocking() -> None:
    bubble = (1000.0, 3000.0)
    events = [host_trace.HostEvent(name="aten::relu", cat="cpu_op", ts_us=1000.0, dur_us=40.0, tid="1")]
    result = host_trace.soft_attribution_for_window(*bubble, events)
    assert result["soft_root_cause_labels"] == ["possible_untraced_host_blocking"]


def test_soft_attribution_host_launch_lag() -> None:
    bubble = (1000.0, 2000.0)
    events = [
        host_trace.HostEvent(name="aten::matmul", cat="cpu_op", ts_us=1000.0, dur_us=200.0, tid="1"),
        host_trace.HostEvent(name="aten::add", cat="cpu_op", ts_us=1100.0, dur_us=200.0, tid="2"),
    ]
    result = host_trace.soft_attribution_for_window(*bubble, events)
    assert result["soft_root_cause_labels"] == ["possible_host_launch_lag"]
    assert result["host_thread_count"] == 2


def test_soft_attribution_python_serialization_hint() -> None:
    """Coverage between 0.05 and 0.1 on a single host thread."""
    bubble = (1000.0, 3000.0)
    events = [host_trace.HostEvent(name="aten::narrow", cat="cpu_op", ts_us=1000.0, dur_us=140.0, tid="1")]
    result = host_trace.soft_attribution_for_window(*bubble, events)
    assert result["soft_root_cause_labels"] == ["possible_python_serialization_or_lock"]


def test_soft_attribution_insufficient_evidence() -> None:
    """Coverage in [0.05, 0.1) spread over >= 2 threads leaves no signal."""
    bubble = (1000.0, 3000.0)
    events = [
        host_trace.HostEvent(name="aten::narrow", cat="cpu_op", ts_us=1000.0, dur_us=70.0, tid="1"),
        host_trace.HostEvent(name="aten::view", cat="cpu_op", ts_us=1200.0, dur_us=70.0, tid="2"),
    ]
    result = host_trace.soft_attribution_for_window(*bubble, events)
    assert result["soft_root_cause_labels"] == ["insufficient_evidence"]


def test_attribute_bubbles_without_trace_degrades_gracefully(tmp_path) -> None:
    bubbles = [
        {"evidence_id": "evd_b0", "rank_id": "rank_0", "start_us": 1000.0, "end_us": 2000.0},
    ]
    rows, status = host_trace.attribute_bubbles(bubbles, {})
    assert rows[0]["soft_attribution"] is None
    assert status["status"] == "missing"
    assert status["bubbles_attributed"] == 0
    assert status["ranks_without_trace"] == ["rank_0"]
    assert status["limitations"], "missing trace must surface a limitation"


def test_attribute_bubbles_with_trace(tmp_path) -> None:
    trace = tmp_path / "trace_view.json"
    _write_trace(
        trace,
        [
            {"ph": "X", "ts": 1000.0, "dur": 800.0, "name": "aclrtMemcpyAsync", "cat": "AscendCL", "pid": 1, "tid": 3},
        ],
    )
    source_index = {
        "sources": [
            {"kind": "trace_view_json", "rank_id": "rank_0", "path": str(trace)},
            {"kind": "kernel_details_csv", "rank_id": "rank_0", "path": "/ignored/kernel_details.csv"},
        ]
    }
    traces = host_trace.trace_view_paths_by_rank(source_index)
    assert traces == {"rank_0": trace}
    bubbles = [{"evidence_id": "evd_b0", "rank_id": "rank_0", "start_us": 1000.0, "end_us": 2000.0}]
    rows, status = host_trace.attribute_bubbles(bubbles, traces)
    attribution = rows[0]["soft_attribution"]
    assert attribution is not None
    assert "possible_sync_or_h2d" in attribution["soft_root_cause_labels"]
    assert attribution["source_path"] == str(trace)
    assert status["status"] == "ok"
    assert status["bubbles_attributed"] == 1
    assert status["limitations"] == []


def test_attribute_bubbles_no_bubbles() -> None:
    rows, status = host_trace.attribute_bubbles([], {})
    assert rows == []
    assert status["status"] == "no_bubbles"


if __name__ == "__main__":
    test_prelaunch_gap_tag_triggers_above_wall_ratio()
    print("ok")
