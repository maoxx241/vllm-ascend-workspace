"""Regression tests for the performance refactors (raw-index default-off,
html lazy raw rows, dedup bisect, category reuse, metrics cache, fused
model-insight scan, report bundle loading).

Each test pins the *equivalence* contract: outputs must match the
pre-refactor behaviour exactly, except for the explicitly documented
raw_kernel_index default change.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import conftest  # noqa: F401 — registers sys.path

from ascend_profile import html_report, metrics, report, summarize
from ascend_profile.model_insights import (
    _EventInsightScan,
    operator_efficiency_rows,
    profile_inferred_model_insights,
)
from ascend_profile.models import NormalizedEvent


def _ne(
    event_id: str,
    rank_id: str = "rank0",
    row_idx: int = 0,
    *,
    name: str = "MatMul",
    task_type: str = "MatMul",
    op_type: str = "aic",
    core: str = "AI_CORE",
    stream: str = "0",
    start: float = 0.0,
    end: float = 10.0,
    wait: float = 0.0,
    categories: tuple[str, ...] = ("compute.matmul",),
    roles: tuple[str, ...] = (),
    shape_features: dict | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id,
        profile_id="p0",
        rank_id=rank_id,
        source_id="src0",
        row_idx=row_idx,
        name_raw=name,
        task_type=task_type,
        accelerator_core=core,
        stream_id=stream,
        start_us=start,
        end_us=end,
        duration_us=max(0.0, end - start),
        wait_us=wait,
        op_categories=categories,
        op_roles=roles,
        shape_features=shape_features or {},
        op_type=op_type,
    )


# ---------------------------------------------------------------------------
# metrics.py: bubble_windows precomputed segments + metrics_for_events cache
# ---------------------------------------------------------------------------


def _gap_events() -> list[NormalizedEvent]:
    # three busy islands: [0,10], [20,30] (two overlapping), [50,60]
    return [
        _ne("e1", row_idx=0, start=0.0, end=10.0),
        _ne("e2", row_idx=1, start=20.0, end=25.0),
        _ne("e3", row_idx=2, start=22.0, end=30.0, stream="1"),
        _ne("e4", row_idx=3, start=50.0, end=60.0),
    ]


def test_bubble_windows_precomputed_segments_identical() -> None:
    events = _gap_events()
    merged = metrics.merge_event_segments(events)
    for limit in (None, 1, 5, 0):
        assert metrics.bubble_windows(events, limit=limit) == metrics.bubble_windows(
            events, limit=limit, segments=merged
        )


def test_metrics_for_events_cache_consistency() -> None:
    events = _gap_events()
    first = metrics.metrics_for_events(events, top_gap_limit=5)
    assert len(first["top_bubbles"]) == 2  # two gaps

    # Mutating the returned dict must not pollute cached state.
    first["category_counts"]["compute.matmul"] = 999
    first["wall_ms"] = -1.0
    first["top_bubbles"].clear()

    second = metrics.metrics_for_events(events, top_gap_limit=5)
    assert second is not first
    assert second["category_counts"]["compute.matmul"] == 4
    assert second["wall_ms"] == 0.06
    assert len(second["top_bubbles"]) == 2

    # A different top_gap_limit on the same window reuses the merge but
    # honours the limit; everything except top_bubbles is identical.
    third = metrics.metrics_for_events(events, top_gap_limit=1)
    assert len(third["top_bubbles"]) == 1
    assert {k: v for k, v in third.items() if k != "top_bubbles"} == {
        k: v for k, v in second.items() if k != "top_bubbles"
    }

    # Same content in a *different* list object recomputes cleanly.
    clone = metrics.metrics_for_events(list(events), top_gap_limit=5)
    assert clone == second


# ---------------------------------------------------------------------------
# html_report.dedup_comm_aiv: bisect scan matches the old linear scan
# ---------------------------------------------------------------------------


def _he(
    rank: str,
    op_type: str,
    start: float,
    end: float,
    *,
    name: str = "kernel",
    row_idx: int = 0,
) -> html_report.Event:
    return html_report.Event(
        event_id=f"evt_{rank}_{row_idx}",
        rank_id=rank,
        source_id="src0",
        row_idx=row_idx,
        name=name,
        task_type="task",
        op_type=op_type,
        accel_core="AI_VECTOR" if op_type == "aiv" else "AI_CORE",
        stream_id="0",
        start_us=start,
        end_us=end,
        duration_us=max(0.0, end - start),
        wait_us=0.0,
        pipeline={},
        shape_signature="",
    )


def _dedup_reference(events: list, iou_threshold: float = 0.9) -> int:
    """The pre-refactor linear-scan implementation, kept as the oracle."""
    by_rank = defaultdict(list)
    for e in events:
        if e.op_type == "communication":
            by_rank[e.rank_id].append(e)
    for rid in by_rank:
        by_rank[rid].sort(key=lambda x: x.start_us)
    dedup = 0
    for e in events:
        if e.op_type == "mix_comm_aiv":
            pass
        elif e.op_type == "aiv":
            nl = (e.name or "").lower()
            if not any(h in nl for h in html_report._COMM_NAME_HINTS):
                continue
        else:
            continue
        for c in by_rank.get(e.rank_id, []):
            if c.end_us < e.start_us:
                continue
            if c.start_us > e.end_us:
                break
            inter = max(0, min(c.end_us, e.end_us) - max(c.start_us, e.start_us))
            union = max(c.end_us, e.end_us) - min(c.start_us, e.start_us)
            iou = inter / union if union > 0 else 0.0
            if iou >= iou_threshold:
                e.redundant = True
                dedup += 1
                break
    return dedup


def _dedup_compare(events: list) -> None:
    ref_events = [
        _he(e.rank_id, e.op_type, e.start_us, e.end_us, name=e.name, row_idx=e.row_idx)
        for e in events
    ]
    ref_count = _dedup_reference(ref_events)
    new_count = html_report.dedup_comm_aiv(events)
    assert new_count == ref_count
    assert [e.redundant for e in events] == [e.redundant for e in ref_events]


def test_dedup_comm_aiv_targeted_cases() -> None:
    # exact overlap -> redundant; partial overlap below IoU -> kept;
    # non-comm aiv name -> skipped; cross-rank isolation.
    _dedup_compare([
        _he("rank0", "communication", 100.0, 200.0, row_idx=0),
        _he("rank0", "mix_comm_aiv", 100.0, 200.0, row_idx=1),
        _he("rank0", "aiv", 150.0, 260.0, name="aclnnAllReduce_flash", row_idx=2),
        _he("rank0", "aiv", 100.0, 200.0, name="Matmul", row_idx=3),
        _he("rank1", "mix_comm_aiv", 100.0, 200.0, row_idx=4),
    ])
    # nested intervals at exactly IoU == 0.9 must match (boundary equality).
    _dedup_compare([
        _he("rank0", "communication", 100.0, 200.0, row_idx=0),
        _he("rank0", "aiv", 105.0, 195.0, name="hcom_allgather_1", row_idx=1),
    ])
    # many early candidates that start AND end before the query window:
    # exercises the prefix-max-end early stop of the backward scan, plus one
    # long-range candidate that outlives everything.
    events = [_he("rank0", "communication", 0.0, 10_000.0, row_idx=0)]
    for i in range(1, 60):
        events.append(_he("rank0", "communication", float(i * 10), float(i * 10 + 5), row_idx=i))
    events.append(_he("rank0", "mix_comm_aiv", 500.0, 520.0, row_idx=60))
    _dedup_compare(events)
    # zero-duration candidates and zero-duration queries (union == 0 guard).
    _dedup_compare([
        _he("rank0", "communication", 100.0, 100.0, row_idx=0),
        _he("rank0", "mix_comm_aiv", 100.0, 100.0, row_idx=1),
        _he("rank0", "mix_comm_aiv", 100.0, 200.0, row_idx=2),
    ])


def test_dedup_comm_aiv_fuzz_matches_reference() -> None:
    import random

    rng = random.Random(20260903)
    for _trial in range(30):
        events = []
        idx = 0
        for rank in ("rank0", "rank1"):
            for _ in range(rng.randint(0, 12)):
                start = rng.uniform(0, 1000)
                end = start + rng.uniform(0, 200)
                events.append(_he(rank, "communication", start, end, row_idx=idx))
                idx += 1
            for _ in range(rng.randint(0, 12)):
                start = rng.uniform(0, 1000)
                end = start + rng.uniform(0, 200)
                op_type = rng.choice(["mix_comm_aiv", "aiv", "aiv", "aic"])
                name = rng.choice(["aclnnAllReduce_x", "Matmul", "hcom_alltoall_1", "FusedInferAttentionScore"])
                events.append(_he(rank, op_type, start, end, name=name, row_idx=idx))
                idx += 1
        rng.shuffle(events)
        _dedup_compare(events)


# ---------------------------------------------------------------------------
# html_report.attention_categories_for_events: reuse normalize categories
# ---------------------------------------------------------------------------


def test_attention_categories_reuse_precomputed_and_fallback() -> None:
    real = html_report.Event(
        event_id="e1",
        rank_id="rank0",
        source_id="src0",
        row_idx=0,
        name="TotallyUnrelatedKernel",  # would never classify as attention
        task_type="",
        op_type="aic",
        accel_core="AI_CORE",
        stream_id="0",
        start_us=0.0,
        end_us=1.0,
        duration_us=1.0,
        wait_us=0.0,
        pipeline={},
        shape_signature="",
        op_categories=("attention.mla", "attention.rope"),
    )
    fake = SimpleNamespace(name="FusedInferAttentionScore", task_type="", accel_core="")
    cats = html_report.attention_categories_for_events([real, fake])
    # precomputed categories are used verbatim (no rules re-derivation),
    # duck-typed fakes still classify through the legacy fallback.
    assert "attention.mla" in cats
    assert "attention.rope" in cats
    assert "attention.flash_score" in cats


# ---------------------------------------------------------------------------
# model_insights: fused single-pass scan equals the per-function scans
# ---------------------------------------------------------------------------


def _insight_events() -> list[NormalizedEvent]:
    return [
        _ne(
            "e1",
            name="lm_head_MatMul",
            shape_features={
                "estimated_work_class": "matmul",
                "estimated_flops": 2 * 4096 * 31040,
                "estimated_bytes": 4096 * 31040 * 2,
                "input_shape_sample": [[1, 4096], [4096, 31040]],
                "output_shape_sample": [[1, 31040]],
            },
        ),
        _ne(
            "e2",
            row_idx=1,
            name="GroupedMatmulExpert",
            categories=("compute.matmul", "moe.expert_matmul"),
            roles=("moe",),
            shape_features={
                "estimated_work_class": "matmul",
                "input_shape_sample": [[8, 4096], [512, 4096, 1024]],
                "output_shape_sample": [[8, 1024]],
            },
        ),
        _ne(
            "e3",
            row_idx=2,
            name="FusedInferAttentionScore",
            task_type="FusedInferAttentionScore",
            categories=("attention.flash_score", "attention.rope"),
            shape_features={
                "estimated_work_class": "attention",
                "input_shape_sample": [[1, 32, 128, 256], [1, 2, 128, 256]],
                "output_shape_sample": [[1, 32, 128, 256]],
            },
        ),
        _ne("e4", row_idx=3, name="hcom_allReduce__1_2_1", task_type="HCOM_ALLREDUCE_", op_type="communication", categories=("comm.hccl",)),
        _ne("e5", row_idx=4, name="MatmulNoShapes", shape_features={"estimated_work_class": "matmul"}),
    ]


def test_model_insights_scan_equivalence() -> None:
    events = _insight_events()
    step_rows = [{"segment_type": "step", "main_layer_count": 2}]
    layer_rows = [{"block_kinds": ["attention", "moe"]}]

    scan = _EventInsightScan(events)
    assert profile_inferred_model_insights(events, step_rows, layer_rows, scan=scan) == profile_inferred_model_insights(
        events, step_rows, layer_rows
    )
    assert operator_efficiency_rows(events, scan=scan) == operator_efficiency_rows(events)


# ---------------------------------------------------------------------------
# summarize: raw_kernel_index default off + opt-in
# ---------------------------------------------------------------------------


def _write_minimal_summarize_fixture(output_dir: Path, event_count: int = 4) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "normalized_event_index.jsonl").open("w", encoding="utf-8") as handle:
        for i in range(event_count):
            handle.write(
                json.dumps(
                    {
                        "event_id": f"evt_0_{i}",
                        "profile_id": "p0",
                        "rank_id": "rank0",
                        "source_id": "src0",
                        "row_idx": i,
                        "name_raw": "MatMul" if i % 2 == 0 else "FusedInferAttentionScore",
                        "task_type": "MatMul",
                        "accelerator_core": "AI_CORE",
                        "stream_id": "0",
                        "start_us": float(i * 10),
                        "end_us": float(i * 10 + 8),
                        "duration_us": 8.0,
                        "wait_us": 0.0,
                        "op_categories": ["compute.matmul"],
                        "op_roles": [],
                        "shape_features": {},
                        "pipeline_us": {},
                        "op_type": "aic",
                    }
                )
                + "\n"
            )
    (output_dir / "step_segments.json").write_text(json.dumps({"step_segments": []}), encoding="utf-8")
    (output_dir / "layer_segments.json").write_text(json.dumps({"layer_segments": []}), encoding="utf-8")
    (output_dir / "block_segments.json").write_text(json.dumps({"block_segments": []}), encoding="utf-8")


def test_summarize_raw_index_default_off(tmp_path: Path) -> None:
    _write_minimal_summarize_fixture(tmp_path)
    manifest = summarize.summarize_profile(tmp_path, scan_cann_hardware=False)

    assert not (tmp_path / "raw_kernel_index.csv").exists()
    assert manifest["files"]["raw_kernel_index"] is None
    assert manifest["counts"]["raw_kernel_rows"] == 0
    note = manifest.get("raw_kernel_index_note") or ""
    assert "write_raw_index" in note


def test_summarize_raw_index_opt_in(tmp_path: Path) -> None:
    _write_minimal_summarize_fixture(tmp_path)
    manifest = summarize.summarize_profile(tmp_path, scan_cann_hardware=False, write_raw_index=True)

    path = tmp_path / "raw_kernel_index.csv"
    assert path.is_file()
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == 4
    assert rows[0]["event_id"] == "evt_0_0"
    assert manifest["files"]["raw_kernel_index"] == "raw_kernel_index.csv"
    assert manifest["counts"]["raw_kernel_rows"] == 4
    assert manifest.get("raw_kernel_index_note") is None


# ---------------------------------------------------------------------------
# report: streamed raw sheet + bundle loading equivalence
# ---------------------------------------------------------------------------


def _write_minimal_report_fixture(output_dir: Path, event_rows: int = 5) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    header = [
        "event_id", "profile_id", "rank_id", "source_id", "row_idx", "name_raw",
        "task_type", "accelerator_core", "stream_id", "start_us", "end_us",
        "duration_us", "wait_us", "op_categories", "op_roles", "shape_signature",
        "shape_features", "pipeline_us", "op_type",
    ]
    with (output_dir / "normalized_event_index.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for i in range(event_rows):
            writer.writerow(
                [
                    f"evt_0_{i}", "p0", "rank0", "src0", i, "MatMul", "MatMul",
                    "AI_CORE", "0", i * 10, i * 10 + 8, 8.0, 0.0, '["compute.matmul"]',
                    "[]", "", "{}", "{}", "aic",
                ]
            )
    (output_dir / "normalize_manifest.json").write_text(
        json.dumps({"profile_root": "/tmp/profile", "rank_count": 1, "event_count": event_rows}),
        encoding="utf-8",
    )
    (output_dir / "summary_manifest.json").write_text(json.dumps({"pipeline_coverage": {}}), encoding="utf-8")
    with (output_dir / "evidence_index.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["evidence_id", "kind", "rank_id", "segment_id", "row_start", "row_end", "summary"])
        writer.writerow(["evd_1", "step_window", "rank0", "seg_1", 0, 4, "Step window seg_1"])
    with (output_dir / "cross_rank_alignment.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["alignment_id", "kind"])
        writer.writerow(["al_1", "step"])
    (output_dir / "diagnosis_findings.json").write_text(
        json.dumps(
            {
                "diagnosis_findings": [
                    {
                        "claim_id": "c1",
                        "finding_type": "demo",
                        "severity": "low",
                        "confidence": "medium",
                        "evidence_ids": ["evd_1"],
                        "summary": "traceable finding",
                    },
                    {
                        "claim_id": "c2",
                        "finding_type": "demo_info",
                        "severity": "info",
                        "confidence": "info",
                        "summary": "info finding needs no evidence",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def test_raw_kernel_sheet_rows_streams_prefix(tmp_path: Path) -> None:
    _write_minimal_report_fixture(tmp_path, event_rows=5)

    rows = report.raw_kernel_sheet_rows(tmp_path, limit=3)
    assert len(rows) == 4  # 3 data rows + truncation marker
    assert rows[0]["event_id"] == "evt_0_0"
    assert rows[2]["row_idx"] == "2"
    assert rows[-1]["event_id"] == "__truncated__"
    assert "normalized_event_index.csv" in str(rows[-1]["name_raw"])

    rows = report.raw_kernel_sheet_rows(tmp_path, limit=5)
    assert len(rows) == 5  # exact fit -> no marker
    assert rows[-1]["event_id"] == "evt_0_4"

    assert report.raw_kernel_sheet_rows(tmp_path / "missing") == []


def test_report_bundle_matches_default_loads(tmp_path: Path) -> None:
    _write_minimal_report_fixture(tmp_path)
    bundle = report._load_report_bundle(tmp_path)

    assert report.markdown_report(tmp_path, "rid", bundle=bundle) == report.markdown_report(tmp_path, "rid")
    assert report.sheet_rows(tmp_path, bundle=bundle) == report.sheet_rows(tmp_path)
    assert report.validate_evidence_chain(tmp_path, bundle=bundle) == report.validate_evidence_chain(tmp_path)

    chain = report.validate_evidence_chain(tmp_path, bundle=bundle)
    assert chain["findings_checked"] == 2
    assert chain["hard_errors"] == []
    assert chain["evidence_rows"] == 1
    assert chain["alignment_rows"] == 1

    # the raw sheet is wired from the streamed normalized index, not raw_kernel_index.csv
    sheets = report.sheet_rows(tmp_path, bundle=bundle)
    assert [row["event_id"] for row in sheets["raw_kernel_index"]] == [f"evt_0_{i}" for i in range(5)]
    assert "name_raw" in sheets["raw_kernel_index"][0]


def test_id_set_from_csv_matches_full_read(tmp_path: Path) -> None:
    _write_minimal_report_fixture(tmp_path)
    ids = report._id_set_from_csv(tmp_path / "evidence_index.csv", "evidence_id")
    assert ids == {"evd_1"}
    assert report._id_set_from_csv(tmp_path / "evidence_index.csv", "no_such_column") == set()
    assert report._id_set_from_csv(tmp_path / "missing.csv", "evidence_id") == set()


if __name__ == "__main__":
    import tempfile

    test_bubble_windows_precomputed_segments_identical()
    test_metrics_for_events_cache_consistency()
    test_dedup_comm_aiv_targeted_cases()
    test_dedup_comm_aiv_fuzz_matches_reference()
    test_attention_categories_reuse_precomputed_and_fallback()
    test_model_insights_scan_equivalence()
    with tempfile.TemporaryDirectory() as td:
        test_summarize_raw_index_default_off(Path(td) / "a")
        test_summarize_raw_index_opt_in(Path(td) / "b")
        test_raw_kernel_sheet_rows_streams_prefix(Path(td) / "c")
        test_report_bundle_matches_default_loads(Path(td) / "d")
        test_id_set_from_csv_matches_full_read(Path(td) / "e")
    print("ok")
