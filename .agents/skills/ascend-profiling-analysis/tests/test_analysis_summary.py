"""Contract tests for the agent-first output layer:

- ``ascend_profile.analysis_summary`` schema (report/analysis_summary.json):
  top-level keys, findings rollup group shape, caps/ordering, null tolerance;
- findings rollup grouping/occurrences/overflow accounting;
- fast-mode skips: ``render_report(skip_xlsx=True)`` drops report.xlsx and
  records it in the manifest; ``summarize_profile(skip_host_trace=True)``
  marks host_trace.status=skipped and keeps bubble soft_attribution=null;
- wrapper fast/full plumbing: ``_common.FAST_PULL_PATHS`` /
  ``REQUIRED_SINGLE_ARTIFACTS_FAST`` and ``profile_analyze`` mode helpers.

All fixtures are tiny synthetic artifacts written at test time; no NPU,
network, or remote access.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import conftest  # noqa: F401 — registers sys.path

import _common as common
import profile_analyze
from ascend_profile import analysis_summary, report, summarize


# ---------------------------------------------------------------------------
# Synthetic output-dir fixture (small but realistic bundle)
# ---------------------------------------------------------------------------


def _write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_summary_fixture(
    output_dir: Path,
    *,
    with_model_config: bool = False,
    model_context_source: str = "profile_operator_fingerprint:operator_match",
    expected_layers: int | None = 61,
    segmentation_mode: str = "model_guided",
    rank1_inventory: str = "[61]",
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    _write_csv(
        output_dir / "normalized_event_index.csv",
        [
            "event_id", "profile_id", "rank_id", "source_id", "row_idx", "name_raw",
            "task_type", "accelerator_core", "stream_id", "start_us", "end_us",
            "duration_us", "wait_us", "op_categories", "op_roles", "shape_signature",
            "shape_features", "pipeline_us", "op_type",
        ],
        [
            [f"evt_0_{i}", "p0", "rank0", "src0", i, "MatMul", "MatMul", "AI_CORE",
             "0", i * 10, i * 10 + 8, 8.0, 0.0, '["compute.matmul"]', "[]", "", "{}", "{}", "aic"]
            for i in range(5)
        ],
    )
    _write_json(
        output_dir / "normalize_manifest.json",
        {
            "profile_root": "/tmp/profile",
            "rank_count": 2,
            "event_count": 6,
            "rank_summaries": [
                {"rank_id": "rank0", "start_us": 1000.0, "end_us": 101000.0, "event_count": 3},
                {"rank_id": "rank1", "start_us": 2000.0, "end_us": 103000.0, "event_count": 3},
            ],
        },
    )
    _write_json(
        output_dir / "source_index.json",
        {
            "sources": [
                {"kind": "kernel_details_csv", "rank_id": "rank0", "path": "/tmp/profile/r0/kernel_details.csv"},
                {"kind": "kernel_details_csv", "rank_id": "rank1", "path": "/tmp/profile/r1/kernel_details.csv"},
            ]
        },
    )
    _write_json(
        output_dir / "analysis_context.json",
        {
            "model_id": "demo-model",
            "model_config": "/tmp/cfg/config.json" if with_model_config else None,
            "hardware_model": None,
            "hardware_profile": None,
            "scan_cann_hardware": False,
        },
    )
    _write_json(
        output_dir / "segment_manifest.json",
        {
            "segment_count": 4,
            "layer_count": 4,
            "model_context": {
                "available": True,
                "confidence": "high",
                "expected_layers": expected_layers,
                "source": model_context_source,
                "model_name": "DemoFamily",
                "limitations": ["demo ctx limitation"],
            },
            "rank_summaries": [
                {"rank_id": "rank0", "segmentation_strategy": {"mode": segmentation_mode}},
                {"rank_id": "rank1", "segmentation_strategy": {"mode": segmentation_mode}},
            ],
        },
    )
    _write_json(
        output_dir / "summary_manifest.json",
        {
            "pipeline_coverage": {"events_ratio": 1.0},
            "host_trace": {"status": "ok", "limitations": []},
            "model_insights": {"features": ["moe", "mla"]},
            "hardware_insights": {"limitations": []},
        },
    )
    _write_json(output_dir / "cross_rank_manifest.json", {})

    _write_csv(
        output_dir / "rank_summary.csv",
        ["rank_id", "wall_ms", "layer_count_inventory", "underfeed_ms"],
        [
            ["rank0", "100.0", "[61]", "10.0"],
            ["rank1", "100.0", rank1_inventory, "10.0"],
        ],
    )
    _write_csv(
        output_dir / "step_summary.csv",
        ["segment_id", "rank_id", "segment_type", "wall_ms", "underfeed_ms", "main_layer_count", "step_family"],
        [
            ["seg_a", "rank0", "step", "40.0", "4.0", "61", "decode"],
            ["seg_b", "rank0", "step", "60.0", "6.0", "61", "decode"],
            ["seg_c", "rank1", "step", "50.0", "5.0", "61", "decode"],
        ],
    )
    _write_csv(
        output_dir / "step_anatomy.csv",
        ["segment_id", "rank_id", "step_wall_ms", "step_bubble_ms", "head_wall_ms", "main_wall_ms", "tail_wall_ms"],
        [
            ["seg_a", "rank0", "40.0", "4.0", "4.0", "32.0", "4.0"],
            ["seg_b", "rank0", "60.0", "6.0", "6.0", "48.0", "6.0"],
            ["seg_c", "rank1", "50.0", "5.0", "5.0", "40.0", "5.0"],
        ],
    )
    _write_csv(
        output_dir / "step_class_summary.csv",
        ["step_class_id", "step_family", "member_count", "wall_ms_mean", "bubble_ratio_mean"],
        [["stp_cls_a", "decode", "3", "50.0", "0.1"]],
    )
    _write_csv(output_dir / "layer_class_summary.csv", ["layer_class_id", "member_count"], [])
    _write_csv(output_dir / "block_class_summary.csv", ["block_class_id", "member_count"], [])
    _write_csv(
        output_dir / "operator_class_summary.csv",
        ["name", "task_type", "op_type", "bound_family", "duration_sum_us", "call_count"],
        [
            ["MatMulV2", "MATMUL", "aic", "cube", "30000.0", "60"],
            ["AddRmsNorm", "RMSNORM", "aiv", "vector", "10000.0", "60"],
            ["HCOM_ALLREDUCE_", "HCOM_ALLREDUCE_", "communication", "communication", "20000.0", "10"],
        ],
    )
    _write_csv(output_dir / "operator_summary.csv", ["name"], [])
    _write_csv(output_dir / "operator_efficiency_summary.csv", ["name"], [])
    _write_csv(
        output_dir / "hccl_class_summary.csv",
        ["hccl_op_kind", "comm_aiv_fused", "duration_sum_us", "rank_skew_ratio", "call_count"],
        [["allreduce", "False", "20000.0", "0.1", "10"]],
    )
    _write_csv(output_dir / "hccl_op_summary.csv", ["hccl_op_kind"], [])
    _write_csv(
        output_dir / "model_inferred_config.csv",
        ["field", "inferred_value", "confidence"],
        [["num_hidden_layers", "61", "high"]],
    )
    _write_csv(
        output_dir / "model_feature_summary.csv",
        ["feature", "confidence"],
        [["moe", "high"], ["mla", "medium"]],
    )
    _write_csv(
        output_dir / "model_candidate_summary.csv",
        ["model_name", "score", "confidence"],
        [
            ["Cand-A", "12.0", "high"],
            ["Cand-B", "10.0", "medium"],
            ["Cand-C", "8.0", "medium"],
            ["Cand-D", "5.0", "low"],
        ],
    )
    _write_csv(
        output_dir / "model_config_overview.csv",
        ["key", "value"],
        [["num_layers", "61"]] if with_model_config else [],
    )
    _write_csv(
        output_dir / "hardware_summary.csv",
        ["key", "value"],
        [
            ["hardware_model", "Ascend910B4"],
            ["fp16_tflops_theoretical", "400.0"],
            ["theoretical_peak_source", "cann_platform_config"],
        ],
    )
    _write_csv(output_dir / "hardware_theoretical_peaks.csv", ["soc_version"], [])
    _write_csv(
        output_dir / "evidence_index.csv",
        ["evidence_id", "kind", "rank_id", "segment_id"],
        [["evd_1", "step_window", "rank0", "seg_a"]],
    )
    _write_csv(
        output_dir / "cross_rank_alignment.csv",
        ["alignment_id", "kind"],
        [["al_1", "step"]],
    )
    _write_json(
        output_dir / "diagnosis_findings.json",
        {
            "diagnosis_findings": [
                {
                    "claim_id": "c1",
                    "finding_type": "device_idle_bubble",
                    "severity": "high",
                    "confidence": "medium",
                    "summary": "Step has heavy device idle bubbles.",
                    "rank_ids": ["rank0"],
                    "evidence_ids": ["evd_1"],
                    "alignment_ids": [],
                    "limitations": ["l1"],
                    "metrics": {"segment_id": "seg_a"},
                },
                {
                    "claim_id": "c2",
                    "finding_type": "device_idle_bubble",
                    "severity": "high",
                    "confidence": "high",
                    "summary": "Step has heavy device idle bubbles.",
                    "rank_ids": ["rank1"],
                    "evidence_ids": ["evd_1"],
                    "alignment_ids": ["al_1"],
                    "limitations": ["l1", "l2"],
                    "metrics": {"segment_id": "seg_b"},
                },
                {
                    "claim_id": "c3",
                    "finding_type": "recurring_bubble_pattern",
                    "severity": "medium",
                    "confidence": "medium",
                    "summary": "Rank shows recurring bubbles.",
                    "rank_ids": ["rank0"],
                    "evidence_ids": ["evd_1"],
                    "limitations": [],
                    "metrics": {},
                },
                {
                    "claim_id": "c4",
                    "finding_type": "demo_info",
                    "severity": "info",
                    "confidence": "info",
                    "summary": "info finding needs no evidence",
                    "rank_ids": [],
                    "metrics": {},
                },
            ]
        },
    )


# ---------------------------------------------------------------------------
# schema contract
# ---------------------------------------------------------------------------

TOP_LEVEL_KEYS = {
    "schema_version",
    "tool",
    "generated_at",
    "identity",
    "layer_validation",
    "kpis",
    "findings",
    "finding_counts",
    "model_brief",
    "hardware_brief",
    "artifacts",
    "stage_timings",
    "limitations",
}

FINDING_GROUP_KEYS = {
    "finding_type",
    "severity",
    "occurrences",
    "summary",
    "affected",
    "evidence_sample",
    "confidence",
    "limitations",
    "knowledge_refs",
}


def test_analysis_summary_schema_contract(tmp_path: Path) -> None:
    _write_summary_fixture(tmp_path)
    summary = analysis_summary.build_analysis_summary(tmp_path, html_status="ok", report_mode="full-raw")

    assert TOP_LEVEL_KEYS <= set(summary), f"missing keys: {TOP_LEVEL_KEYS - set(summary)}"
    assert summary["schema_version"] == 1
    assert summary["tool"] == "ascend-profiling-analysis"

    identity = summary["identity"]
    assert identity["profile_root"] == "/tmp/profile"
    assert identity["rank_count"] == 2
    assert identity["event_count"] == 6
    assert identity["capture"] == {"start_us": 1000.0, "end_us": 103000.0, "wall_ms": 102.0}
    assert identity["source"] == "csv"
    assert identity["model"]["model_id"] == "demo-model"
    assert identity["model"]["candidate_names"] == ["Cand-A", "Cand-B", "Cand-C"]

    lv = summary["layer_validation"]
    assert lv["status"] == "ok"
    assert lv["expected_layers"] == 61
    assert lv["expected_source"] == "fingerprint"
    assert lv["detected_layers"] == {"min": 61, "max": 61, "per_rank_outliers": []}
    assert lv["layers_match"] is True
    assert lv["per_rank_consistent"] is True
    assert lv["segmentation_mode"] == "model_guided"
    assert lv["confidence"] == "high"

    kpis = summary["kpis"]
    assert kpis["step_wall_ms"] == {"count": 3, "mean": 50.0, "p50": 50.0, "p90": 60.0}
    assert kpis["step_anatomy_ms_mean"] == {"head": 5.0, "main": 40.0, "tail": 5.0, "bubble": 5.0}
    assert kpis["bubble_share_of_wall"] == 0.1
    assert kpis["comm_share_of_wall"] == 0.1  # 20ms comm / 200ms rank wall
    assert kpis["bound_family_wall_ms"] == {"communication": 20.0, "cube": 30.0, "vector": 10.0}
    assert kpis["top_step_classes"] == [
        {"class_id": "stp_cls_a", "members": 3, "wall_ms_mean": 50.0, "bubble_ratio_mean": 0.1}
    ]
    assert kpis["top_operator_classes"][0] == {
        "name": "MatMulV2", "duration_ms_sum": 30.0, "share_of_wall": 0.15, "bound_family": "cube",
    }
    assert [row["name"] for row in kpis["top_operator_classes"]] == ["MatMulV2", "AddRmsNorm"]
    assert kpis["top_hccl_kinds"] == [
        {"kind": "allreduce", "duration_ms_sum": 20.0, "rank_skew_ratio": 0.1}
    ]

    assert summary["model_brief"]["features"] == ["moe", "mla"]
    assert summary["model_brief"]["inferred_layers"] == 61
    assert summary["model_brief"]["candidate_names"] == ["Cand-A", "Cand-B", "Cand-C"]
    assert summary["hardware_brief"]["hardware_model"] == "Ascend910B4"
    assert "fp16_tflops=400.0" in summary["hardware_brief"]["theoretical_peaks_note"]
    assert summary["artifacts"]["report_html"] == "report/report.html"
    assert summary["artifacts"]["analysis_summary"] == "report/analysis_summary.json"

    for group in summary["findings"]:
        assert FINDING_GROUP_KEYS <= set(group)
        assert group["knowledge_refs"] == []
        assert set(group["affected"]) == {"rank_ids", "segment_ids"}

    encoded = json.dumps(summary, ensure_ascii=False).encode("utf-8")
    assert len(encoded) < 50_000, f"summary too large: {len(encoded)} bytes"


def test_analysis_summary_null_tolerance_on_empty_dir(tmp_path: Path) -> None:
    """No artifacts at all: build must not raise; missing data is null."""
    summary = analysis_summary.build_analysis_summary(tmp_path)

    assert summary["identity"]["source"] is None
    assert summary["identity"]["capture"]["wall_ms"] is None
    assert summary["identity"]["model"]["model_id"] is None
    lv = summary["layer_validation"]
    assert lv["status"] == "unknown"
    assert lv["expected_layers"] is None
    assert lv["expected_source"] == "unknown"
    assert lv["detected_layers"]["min"] is None
    assert lv["layers_match"] is None
    assert lv["segmentation_mode"] is None
    assert summary["kpis"]["step_wall_ms"] is None
    assert summary["kpis"]["step_anatomy_ms_mean"] is None
    assert summary["kpis"]["bound_family_wall_ms"] is None
    assert summary["kpis"]["comm_share_of_wall"] is None
    assert summary["findings"] == []
    assert summary["finding_counts"]["total"] == 0
    assert summary["hardware_brief"]["hardware_model"] is None
    assert summary["stage_timings"] is None
    assert summary["limitations"], "null fields must be explained in limitations"


def test_layer_validation_expected_layers_priority(tmp_path: Path) -> None:
    # config.json (via analysis_context + model_config_overview) beats the
    # fingerprint-catalog / operator-match model context.
    _write_summary_fixture(tmp_path, with_model_config=True)
    lv = analysis_summary.build_analysis_summary(tmp_path)["layer_validation"]
    assert lv["expected_layers"] == 61
    assert lv["expected_source"] == "config"

    _write_summary_fixture(tmp_path, model_context_source="model_fingerprint_catalog:profile_operator")
    lv = analysis_summary.build_analysis_summary(tmp_path)["layer_validation"]
    assert lv["expected_source"] == "knowledge"

    _write_summary_fixture(tmp_path, expected_layers=None)
    lv = analysis_summary.build_analysis_summary(tmp_path)["layer_validation"]
    assert lv["expected_layers"] is None
    assert lv["expected_source"] == "unknown"
    assert lv["layers_match"] is None
    assert any("expected layer count unknown" in item for item in lv["limitations"])


def test_layer_validation_degraded_states(tmp_path: Path) -> None:
    _write_summary_fixture(tmp_path, segmentation_mode="exact_cover_knowledge_miss")
    lv = analysis_summary.build_analysis_summary(tmp_path)["layer_validation"]
    assert lv["status"] == "degraded"
    assert lv["segmentation_mode"] == "exact_cover_knowledge_miss"

    _write_summary_fixture(tmp_path, rank1_inventory="[61, 122]")
    lv = analysis_summary.build_analysis_summary(tmp_path)["layer_validation"]
    assert lv["per_rank_consistent"] is False
    assert lv["detected_layers"]["min"] == 61
    assert lv["detected_layers"]["max"] == 122
    assert lv["detected_layers"]["per_rank_outliers"] == [
        {"rank_id": "rank1", "layer_count_inventory": [61, 122]}
    ]
    assert lv["status"] == "degraded"


# ---------------------------------------------------------------------------
# findings rollup
# ---------------------------------------------------------------------------


def test_rollup_groups_and_orders(tmp_path: Path) -> None:
    _write_summary_fixture(tmp_path)
    findings = report.finding_rows(tmp_path)
    groups, counts = analysis_summary.rollup_findings(findings)

    assert counts["total"] == 4
    assert counts["by_type"] == {"device_idle_bubble": 2, "recurring_bubble_pattern": 1, "demo_info": 1}
    assert counts["by_severity"] == {"high": 2, "medium": 1, "info": 1}
    assert counts["rollup_groups"] == 3
    assert counts["rollup_overflow"] == 0

    # high severity group first, then medium, then info.
    assert [g["finding_type"] for g in groups] == ["device_idle_bubble", "recurring_bubble_pattern", "demo_info"]
    top = groups[0]
    assert top["occurrences"] == 2
    assert top["affected"]["rank_ids"] == ["rank0", "rank1"]
    assert top["affected"]["segment_ids"] == ["seg_a", "seg_b"]
    assert top["evidence_sample"] == ["evd_1", "al_1"]
    assert top["confidence"] == "high"  # max confidence across members
    assert top["limitations"] == ["l1", "l2"]  # deduped
    assert top["knowledge_refs"] == []


def test_rollup_cap_and_overflow() -> None:
    findings = [
        {
            "finding_type": f"type_{i}",
            "severity": "medium",
            "confidence": "low",
            "summary": f"distinct summary {i}",
            "rank_ids": [],
        }
        for i in range(12)
    ]
    groups, counts = analysis_summary.rollup_findings(findings, limit=10)
    assert len(groups) == 10
    assert counts["rollup_groups"] == 12
    assert counts["rollup_overflow"] == 2
    assert counts["total"] == 12

    groups, counts = analysis_summary.rollup_findings(findings)
    assert len(groups) == 12
    assert counts["rollup_overflow"] == 0

    groups, counts = analysis_summary.rollup_findings([])
    assert groups == []
    assert counts == {
        "total": 0, "by_type": {}, "by_severity": {}, "rollup_groups": 0, "rollup_overflow": 0,
    }


def test_rollup_severity_beats_occurrences() -> None:
    findings = [
        {"finding_type": "noisy_low", "severity": "low", "summary": "s", "confidence": "low"}
        for _ in range(9)
    ] + [
        {"finding_type": "rare_high", "severity": "high", "summary": "s2", "confidence": "high"}
    ]
    groups, _ = analysis_summary.rollup_findings(findings)
    assert groups[0]["finding_type"] == "rare_high"
    assert groups[1]["finding_type"] == "noisy_low"


# ---------------------------------------------------------------------------
# fast-mode skips: report skip_xlsx / summarize skip_host_trace
# ---------------------------------------------------------------------------


def test_render_report_full_mode_writes_xlsx_and_summary(tmp_path: Path) -> None:
    _write_summary_fixture(tmp_path)
    manifest = report.render_report(tmp_path, skip_html=True)

    assert (tmp_path / "report" / "report.xlsx").is_file()
    assert (tmp_path / "report" / "report.md").is_file()
    assert manifest["xlsx_status"] == "ok"
    assert manifest["skip_xlsx"] is False
    assert manifest["files"]["xlsx"] == "report.xlsx"
    assert manifest["files"]["analysis_summary"] == "analysis_summary.json"
    assert isinstance(manifest["sheet_map"], dict) and manifest["sheet_map"]
    assert manifest["host_trace_status"] == "ok"

    summary_path = tmp_path / "report" / "analysis_summary.json"
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["schema_version"] == 1
    assert summary["identity"]["profile_root"] == "/tmp/profile"
    # skip_html forces the summary HTML stub, so no usable HTML artifact.
    assert summary["artifacts"]["report_html"] is None


def test_render_report_skip_xlsx_fast_mode(tmp_path: Path) -> None:
    _write_summary_fixture(tmp_path)
    manifest = report.render_report(tmp_path, skip_xlsx=True, report_mode="summary")

    assert not (tmp_path / "report" / "report.xlsx").exists()
    assert (tmp_path / "report" / "report.md").is_file()
    # HTML stub still written in summary mode.
    assert (tmp_path / "report" / "report.html").is_file()
    assert manifest["html_status"] == "skipped"
    assert manifest["xlsx_status"] == "skipped"
    assert manifest["skip_xlsx"] is True
    assert manifest["files"]["xlsx"] is None
    assert manifest["sheet_map"] is None

    summary = json.loads((tmp_path / "report" / "analysis_summary.json").read_text(encoding="utf-8"))
    assert summary["report_mode"] == "summary"
    assert any("--skip-xlsx" in item for item in summary["limitations"])


def _write_summarize_fixture(output_dir: Path) -> None:
    """Events with one internal gap, covered by a single step segment."""
    output_dir.mkdir(parents=True, exist_ok=True)
    events = [
        ("evt_0_0", 0, 0.0, 10.0),
        ("evt_0_1", 1, 10.0, 20.0),
        ("evt_0_2", 2, 1000.0, 1010.0),  # 980us internal bubble
    ]
    with (output_dir / "normalized_event_index.jsonl").open("w", encoding="utf-8") as handle:
        for event_id, row_idx, start, end in events:
            handle.write(
                json.dumps(
                    {
                        "event_id": event_id,
                        "profile_id": "p0",
                        "rank_id": "rank0",
                        "source_id": "src0",
                        "row_idx": row_idx,
                        "name_raw": "MatMul",
                        "task_type": "MatMul",
                        "accelerator_core": "AI_CORE",
                        "stream_id": "0",
                        "start_us": start,
                        "end_us": end,
                        "duration_us": end - start,
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
    _write_json(
        output_dir / "step_segments.json",
        {
            "step_segments": [
                {
                    "segment_id": "seg_s0",
                    "rank_id": "rank0",
                    "segment_type": "step",
                    "complete": True,
                    "row_start": 0,
                    "row_end": 2,
                    "start_us": 0.0,
                    "end_us": 1010.0,
                }
            ]
        },
    )
    _write_json(output_dir / "layer_segments.json", {"layer_segments": []})
    _write_json(output_dir / "block_segments.json", {"block_segments": []})
    _write_json(
        output_dir / "source_index.json",
        {"sources": [{"kind": "kernel_details_csv", "rank_id": "rank0", "path": "/tmp/none/kernel_details.csv"}]},
    )


def _bubble_rows(output_dir: Path) -> list[dict]:
    path = output_dir / "evidence" / "bubble_windows.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_summarize_skip_host_trace_marks_status_and_nulls_attribution(tmp_path: Path) -> None:
    _write_summarize_fixture(tmp_path)
    manifest = summarize.summarize_profile(tmp_path, scan_cann_hardware=False, skip_host_trace=True)

    host_trace = manifest["host_trace"]
    assert host_trace["status"] == "skipped"
    assert host_trace["bubbles_attributed"] == 0
    assert any("--skip-host-trace" in item for item in host_trace["limitations"])

    bubbles = _bubble_rows(tmp_path)
    assert bubbles, "fixture must produce at least one bubble window"
    assert all(row.get("soft_attribution") is None for row in bubbles)

    # The report Limitations section surfaces the skip note via the manifest.
    markdown = report.markdown_report(tmp_path, "rid")
    assert "--skip-host-trace" in markdown


def test_summarize_default_still_runs_host_trace_path(tmp_path: Path) -> None:
    _write_summarize_fixture(tmp_path)
    manifest = summarize.summarize_profile(tmp_path, scan_cann_hardware=False)
    # No trace_view.json registered -> the historical graceful-degradation
    # status, not the new fast-mode skip.
    assert manifest["host_trace"]["status"] == "missing"


# ---------------------------------------------------------------------------
# wrapper fast/full plumbing
# ---------------------------------------------------------------------------


def test_fast_pull_paths_are_lean() -> None:
    fast = set(common.FAST_PULL_PATHS)
    for required in (
        "manifest.json",
        "normalize_manifest.json",
        "segment_manifest.json",
        "classify_manifest.json",
        "summary_manifest.json",
        "cross_rank_manifest.json",
        "diagnosis_findings.json",
        "rank_summary.csv",
        "step_summary.csv",
        "step_class_summary.csv",
        "layer_class_summary.csv",
        "block_class_summary.csv",
        "operator_class_summary.csv",
        "hccl_class_summary.csv",
        "report/manifest.json",
        "report/report.md",
        "report/analysis_summary.json",
    ):
        assert required in fast, f"FAST_PULL_PATHS missing {required}"
    for excluded in (
        "evidence_index.csv",
        "raw_kernel_index.csv",
        "normalized_event_index.csv",
        "cross_rank_alignment.csv",
        "cross_rank_alignment.json",
        "operator_summary.csv",
        "operator_efficiency_summary.csv",
        "step_anatomy.csv",
        "layer_summary.csv",
        "block_summary.csv",
        "step_segments.json",
        "report/report.xlsx",
        "report/report.html",
    ):
        assert excluded not in fast, f"FAST_PULL_PATHS must not pull {excluded}"
    # The full list is a strict superset minus nothing: fast is a subset.
    assert fast <= set(common.LIGHTWEIGHT_PULL_PATHS)
    # Both modes pull the summary so the wrapper can embed it on stdout.
    assert "report/analysis_summary.json" in common.LIGHTWEIGHT_PULL_PATHS


def test_fast_required_artifacts_replace_xlsx_with_summary() -> None:
    full = set(common.REQUIRED_SINGLE_ARTIFACTS)
    fast = set(common.REQUIRED_SINGLE_ARTIFACTS_FAST)
    assert "report/report.xlsx" in full and "report/report.xlsx" not in fast
    assert "report/analysis_summary.json" in fast
    assert fast - full == {"report/analysis_summary.json"}
    assert full - fast == {"report/report.xlsx"}
    # Stage-window routing: fast only changes the report end-stage set.
    assert profile_analyze._required_artifacts_for("report", "fast") == common.REQUIRED_SINGLE_ARTIFACTS_FAST
    assert profile_analyze._required_artifacts_for("report", "full") == common.REQUIRED_SINGLE_ARTIFACTS
    assert (
        profile_analyze._required_artifacts_for("summarize", "fast")
        == profile_analyze._required_artifacts_for("summarize", "full")
    )


def test_mode_analyze_flags() -> None:
    assert profile_analyze._mode_analyze_flags("fast", skip_html=False, report_mode="full-raw") == [
        "--skip-xlsx",
        "--skip-host-trace",
        "--report-mode",
        "summary",
    ]
    assert profile_analyze._mode_analyze_flags("full", skip_html=False, report_mode="full-raw") == [
        "--report-mode",
        "full-raw",
    ]
    assert profile_analyze._mode_analyze_flags("full", skip_html=True, report_mode="summary") == [
        "--skip-html",
        "--report-mode",
        "summary",
    ]


def test_pull_paths_for_mode() -> None:
    assert profile_analyze._pull_paths_for_mode("fast") == common.FAST_PULL_PATHS
    assert profile_analyze._pull_paths_for_mode("full") == common.LIGHTWEIGHT_PULL_PATHS


def test_mode_arg_defaults_to_fast() -> None:
    parser = profile_analyze._build_parser()
    args = parser.parse_args(["--remote-profile-root", "/x"])
    assert args.mode == "fast"
    args = parser.parse_args(["--remote-profile-root", "/x", "--mode", "full"])
    assert args.mode == "full"


def test_read_local_analysis_summary(tmp_path: Path) -> None:
    assert profile_analyze._read_local_analysis_summary(tmp_path) is None

    summary_dir = tmp_path / "report"
    summary_dir.mkdir()
    (summary_dir / "analysis_summary.json").write_text('{"schema_version": 1}', encoding="utf-8")
    assert profile_analyze._read_local_analysis_summary(tmp_path) == {"schema_version": 1}

    (summary_dir / "analysis_summary.json").write_text("not json{", encoding="utf-8")
    assert profile_analyze._read_local_analysis_summary(tmp_path) is None


def test_analyze_and_sweep_parsers_have_skip_flags() -> None:
    from ascend_profile import analyze, report as report_module, summarize as summarize_module, sweep

    def _flags(parser) -> set[str]:
        return {s for action in parser._actions for s in action.option_strings}  # type: ignore[attr-defined]

    analyze_flags = _flags(analyze.build_parser())
    assert "--skip-xlsx" in analyze_flags
    assert "--skip-host-trace" in analyze_flags
    assert "--skip-xlsx" in _flags(report_module.build_parser())
    assert "--skip-host-trace" in _flags(summarize_module.build_parser())
    sweep_flags = _flags(sweep.build_parser())
    assert "--skip-xlsx" in sweep_flags and "--no-skip-xlsx" in sweep_flags
    assert "--skip-host-trace" in sweep_flags and "--no-skip-host-trace" in sweep_flags


def test_sweep_layer_validation_status_enrichment(tmp_path: Path) -> None:
    """sweep._layer_validation_status reads the per-root summary, or None."""
    from ascend_profile import sweep

    assert sweep._layer_validation_status(tmp_path) is None
    _write_json(
        tmp_path / "report" / "analysis_summary.json",
        {"layer_validation": {"status": "degraded"}},
    )
    assert sweep._layer_validation_status(tmp_path) == "degraded"
    (tmp_path / "report" / "analysis_summary.json").write_text("not json{", encoding="utf-8")
    assert sweep._layer_validation_status(tmp_path) is None


if __name__ == "__main__":
    import tempfile

    test_rollup_cap_and_overflow()
    test_rollup_severity_beats_occurrences()
    test_fast_pull_paths_are_lean()
    test_fast_required_artifacts_replace_xlsx_with_summary()
    test_mode_analyze_flags()
    test_pull_paths_for_mode()
    test_mode_arg_defaults_to_fast()
    test_analyze_and_sweep_parsers_have_skip_flags()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        test_analysis_summary_schema_contract(root / "a")
        test_analysis_summary_null_tolerance_on_empty_dir(root / "b")
        test_layer_validation_expected_layers_priority(root / "c")
        test_layer_validation_degraded_states(root / "d")
        test_rollup_groups_and_orders(root / "e")
        test_render_report_full_mode_writes_xlsx_and_summary(root / "f")
        test_render_report_skip_xlsx_fast_mode(root / "g")
        test_summarize_skip_host_trace_marks_status_and_nulls_attribution(root / "h")
        test_summarize_default_still_runs_host_trace_path(root / "i")
        test_read_local_analysis_summary(root / "j")
        test_sweep_layer_validation_status_enrichment(root / "k")
    print("ok")
