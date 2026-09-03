"""Tests for the v2 HTML report renderer (thin shell + gzipped assets).

Covers: asset completeness/integrity (every manifest-referenced file exists,
gunzips, parses, and matches its view schema), manifest ↔ shell consistency,
single-file embedding + threshold refusal, file:// and DecompressionStream
detection hints, analysis_summary.json consumption (with graceful fallback),
KPI/view-count parity with the legacy renderer, and report.py renderer wiring.
"""
from __future__ import annotations

import base64
import gzip
import json
import re
from pathlib import Path

import pytest

import conftest  # noqa: F401 — registers sys.path

from ascend_profile import html_report, report
from ascend_profile.html_report_v2 import assets as v2_assets
from ascend_profile.html_report_v2 import build_html_report_v2, payloads

RANK = "dp0_tp0_rank0"
CLS_A = "stp_cls_aaaa"
STEPS = [
    # (segment_id, class_id, row_start, row_end, wall_ms)
    ("seg_a0", CLS_A, 0, 19, 2.0),
    ("seg_a1", CLS_A, 20, 39, 4.0),
    ("seg_c0", "", 40, 59, 3.0),  # unclassified step → _unclassified bucket
]
EVENTS_PER_STEP = 20
N_EVENTS = 60


def _event_row(row_idx: int, *, name: str, op_type: str, categories: list[str]) -> dict:
    base = 1_000_000.0
    start = base + row_idx * 100.0
    duration = 30.0 + (row_idx % 5) * 10
    return {
        "event_id": f"evt_{row_idx}",
        "profile_id": "p0",
        "rank_id": RANK,
        "source_id": "src0",
        "row_idx": row_idx,
        "name_raw": name,
        "task_type": name.upper().split("_")[0],
        "accelerator_core": "AI_CORE" if op_type == "aic" else "AI_VECTOR_CORE",
        "stream_id": str(7 + (row_idx % 3)),
        "start_us": start,
        "end_us": start + duration,
        "duration_us": duration,
        "wait_us": float(row_idx % 3),
        "op_categories": categories,
        "op_roles": [],
        "shape_signature": "",
        "shape_features": {},
        "pipeline_us": {"aic_mac_time(us)": duration * 0.6, "aic_mte2_time(us)": duration * 0.2},
        "op_type": op_type,
    }


def _kernel_details_rows() -> list[dict]:
    rows = []
    for i in range(N_EVENTS):
        row = {f: "" for f in html_report.RAW_KD_FIELDS}
        row.update({
            "Name": f"aclnnMatmul_MatMul_{i}" if i % 4 else "FusedInferAttentionScore",
            "Type": "MATMUL",
            "Stream ID": str(7 + (i % 3)),
            "Start Time(us)": f"{1_000_000.0 + i * 100.0:.1f}",
            "Duration(us)": f"{30.0 + (i % 5) * 10:.1f}",
            "Wait Time(us)": f"{float(i % 3):.1f}",
            "Block Dim": "8",
            "Input Shapes": "1,16,64;16,64",
            "Input Data Types": "DT_FLOAT16;DT_FLOAT16",
            "Input Formats": "ND;ND",
            "Output Shapes": "1,16,64",
            "Output Data Types": "DT_FLOAT16",
            "Output Formats": "ND",
            "aic_mac_time(us)": "18.0",
            "aic_mac_ratio": "0.62",
            "aic_mte2_time(us)": "6.0",
            "aic_mte2_ratio": "0.21",
            "cube_utilization(%)": "71.5",
        })
        rows.append(row)
    return rows


def make_root(tmp_path: Path) -> Path:
    """A minimal but complete analysis root (1 rank, 3 steps, 2 layers/step)."""
    root = tmp_path / "analysis"
    root.mkdir(parents=True)
    from ascend_profile import store

    # events: per step, layer0 = rows [rs, rs+9] (attention), layer1 = [rs+10, rs+19] (ffn)
    event_rows = []
    for i in range(N_EVENTS):
        in_attention = (i % EVENTS_PER_STEP) < 10
        if i % 6 == 5:
            name, op_type, cats = f"hcom_allReduce__{i}_0_1", "communication", ["communication.allreduce"]
        elif in_attention:
            name = "FusedInferAttentionScore" if i % 4 == 0 else f"aclnnMatmul_MatMul_{i}"
            op_type = "mix_cv" if i % 4 == 0 else "aic"
            cats = ["attention.flash_score"] if i % 4 == 0 else ["compute.matmul"]
        else:
            name, op_type, cats = f"aclnnGroupedMatmul_GMM_{i}", "mix_cv", ["moe.expert_matmul"]
        event_rows.append(_event_row(i, name=name, op_type=op_type, categories=cats))
    store.write_csv(root / "normalized_event_index.csv", event_rows)

    step_seg_rows = []
    layer_seg_rows = []
    block_seg_rows = []
    for seg_id, cls_id, rs, re_, wall in STEPS:
        step_seg_rows.append({
            "segment_id": seg_id, "rank_id": RANK, "segment_type": "step", "complete": True,
            "row_start": rs, "row_end": re_,
            "start_us": 1_000_000.0 + rs * 100.0, "end_us": 1_000_000.0 + re_ * 100.0 + 100.0,
            "wall_ms": wall, "step_family": "attention_moe_workload",
        })
        for li, (lrs, lre) in enumerate(((rs, rs + 9), (rs + 10, rs + 19))):
            layer_id = f"layer_{seg_id}_{li}"
            layer_seg_rows.append({
                "layer_id": layer_id, "segment_id": seg_id, "rank_id": RANK,
                "layer_index": li, "layer_role": "main",
                "row_start": lrs, "row_end": lre,
                "start_us": 1_000_000.0 + lrs * 100.0, "end_us": 1_000_000.0 + lre * 100.0 + 100.0,
                "boundary_source": "unit_test", "structure_signature": "sig",
            })
            mid = (lrs + lre) // 2
            block_seg_rows.append({
                "block_id": f"blk_{layer_id}_a", "layer_id": layer_id, "segment_id": seg_id,
                "rank_id": RANK, "block_kind": "attention", "block_index": 0,
                "row_start": lrs, "row_end": mid,
                "start_us": 1_000_000.0 + lrs * 100.0, "end_us": 1_000_000.0 + mid * 100.0 + 100.0,
            })
            block_seg_rows.append({
                "block_id": f"blk_{layer_id}_f", "layer_id": layer_id, "segment_id": seg_id,
                "rank_id": RANK, "block_kind": "ffn", "block_index": 1,
                "row_start": mid + 1, "row_end": lre,
                "start_us": 1_000_000.0 + (mid + 1) * 100.0, "end_us": 1_000_000.0 + lre * 100.0 + 100.0,
            })
    store.write_json(root / "step_segments.json", {"step_segments": step_seg_rows})
    store.write_json(root / "layer_segments.json", {"layer_segments": layer_seg_rows})
    store.write_json(root / "block_segments.json", {"block_segments": block_seg_rows})

    store.write_csv(root / "rank_summary.csv", [{
        "rank_id": RANK, "step_count": 3, "segment_count": 3, "layer_count_inventory": "[2]",
        "event_count": N_EVENTS, "row_start": 0, "row_end": N_EVENTS - 1,
        "start_us": 1_000_000.0, "end_us": 1_006_000.0,
        "wall_ms": 6.0, "busy_union_ms": 5.0, "underfeed_ratio": 0.01,
    }])
    store.write_csv(root / "step_summary.csv", [{
        "segment_id": seg_id, "rank_id": RANK, "segment_type": "step", "complete": "True",
        "step_family": "attention_moe_workload", "row_start": rs, "row_end": re_,
        "main_layer_count": 2, "has_attention": "True", "has_moe": "True",
        "start_us": 1_000_000.0 + rs * 100.0, "end_us": 1_000_000.0 + re_ * 100.0 + 100.0,
        "wall_ms": wall, "busy_union_ms": wall * 0.9, "bubble_ratio": 0.1,
        "step_class_id": cls_id,
    } for seg_id, cls_id, rs, re_, wall in STEPS])
    store.write_csv(root / "step_class_summary.csv", [{
        "step_class_id": CLS_A, "step_family": "attention_moe_workload", "main_layer_count": 2,
        "member_count": 2, "rank_count": 1, "wall_ms_sum": 6.0, "wall_ms_mean": 3.0,
        "wall_ms_p50": 3.0, "wall_ms_p90": 4.0, "bubble_ms_mean": 0.3,
    }])
    store.write_csv(root / "layer_class_summary.csv", [{
        "layer_class_id": "lyr_cls_x", "block_kinds": '["attention","ffn"]', "member_count": 6,
        "rank_count": 1, "wall_ms_sum": 12.0, "wall_ms_mean": 2.0, "wall_ms_p50": 2.0,
        "wall_ms_p90": 2.5, "bubble_ms_mean": 0.1,
    }])
    store.write_csv(root / "block_class_summary.csv", [{
        "block_class_id": "blk_cls_x", "block_kind": "attention", "member_count": 6,
        "rank_count": 1, "wall_ms_sum": 9.0, "wall_ms_mean": 1.5, "wall_ms_p50": 1.5,
        "wall_ms_p90": 1.9, "bound_family": "cube", "comm_share_mean": 0.1,
    }])

    store.write_json(root / "diagnosis_findings.json", {"diagnosis_findings": [
        {
            "claim_id": "claim_1", "finding_type": "device_idle_bubble", "severity": "high",
            "confidence": "high", "summary": "step 内存在 device idle 空泡",
            "evidence_ids": ["evd_step_a0"], "alignment_ids": [], "counter_evidence_ids": [],
            "rank_ids": [RANK], "metrics": {"segment_id": "seg_a0", "bubble_ms": 0.3},
            "limitations": [],
        },
        {
            "claim_id": "claim_2", "finding_type": "device_idle_bubble", "severity": "high",
            "confidence": "medium", "summary": "step 内存在 device idle 空泡",
            "evidence_ids": ["evd_layer_a0"], "alignment_ids": [], "counter_evidence_ids": [],
            "rank_ids": [RANK], "metrics": {"segment_id": "seg_a1", "bubble_ms": 0.2},
            "limitations": [],
        },
    ]})
    store.write_csv(root / "evidence_index.csv", [
        {"evidence_id": "evd_step_a0", "kind": "step_window", "rank_id": RANK,
         "segment_id": "seg_a0", "row_start": 0, "row_end": 19, "summary": "Step window seg_a0", "layer_id": ""},
        {"evidence_id": "evd_layer_a0", "kind": "layer_window", "rank_id": RANK,
         "segment_id": "seg_a1", "row_start": 20, "row_end": 29,
         "summary": "Layer window", "layer_id": "layer_seg_a1_0"},
    ])
    store.write_json(root / "manifest.json", {"analysis_stage": "unit-test"})
    store.write_json(root / "segment_manifest.json", {
        "model_context": {"available": True, "expected_layers": 2, "confidence": "high",
                          "source": "config"},
        "rank_summaries": [{"rank_id": RANK, "segmentation_strategy": {"mode": "exact_structural_anchor"}}],
    })

    kd_path = tmp_path / "kernel_details.csv"
    store.write_csv(kd_path, _kernel_details_rows())
    store.write_json(root / "source_index.json", {"sources": [{
        "kind": "kernel_details_csv", "path": str(kd_path), "source_id": "src0",
        "rank_id": RANK, "row_start": 0, "row_end": N_EVENTS - 1, "row_base": "zero_based",
    }]})
    return root


def _read_gz(path: Path):
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def _boot_var(html: str, name: str):
    m = re.search(rf"window\.{name}=(.*?);(?:\n|$)", html)
    assert m, f"boot variable {name} missing from shell HTML"
    return json.loads(m.group(1).replace("<\\/", "</"))


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Build the v2 report once per module; return (root, report_dir, html)."""
    root = make_root(tmp_path_factory.mktemp("v2root"))
    out = root / "report" / "report.html"
    build_html_report_v2(root, out)
    return root, out.parent, out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# asset integrity
# ---------------------------------------------------------------------------


def test_assets_complete_and_valid(built):
    root, report_dir, _html = built
    manifest = json.loads((report_dir / "assets" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == payloads.MANIFEST_SCHEMA
    assert manifest["html_renderer"] == "v2"
    assert manifest["single_file"] is False
    assert manifest["assets"], "manifest must index at least one asset"

    for key, entry in manifest["assets"].items():
        path = report_dir / entry["file"]
        assert path.is_file(), f"manifest references missing asset {key} → {entry['file']}"
        assert path.stat().st_size == entry["bytes"], f"size drift for {entry['file']}"
        data = _read_gz(path)
        assert data.get("schema") == entry["schema"], f"schema mismatch for {key}"

        if key.startswith("l2/"):
            assert data["schema"] == payloads.L2_SCHEMA
            for field in ("class_id", "steps", "step_detail", "rep_segment_id",
                          "wall_ms_sum", "family_label", "has_l3"):
                assert field in data, f"l2 asset {key} missing {field}"
            for step in data["steps"]:
                for field in ("seg", "rank", "rank_id", "idx", "wall", "bubble_pct", "fam"):
                    assert field in step
                detail = data["step_detail"][step["seg"]]
                for field in ("phase", "xrank", "kernels", "layers", "l3_target"):
                    assert field in detail
        elif key.startswith("l3/"):
            assert data["schema"] == payloads.L3_SCHEMA
            for field in ("layer_key", "layer_index", "role", "events", "bubbles",
                          "layer_busy_us", "step_busy_us", "rep_segment_id"):
                assert field in data, f"l3 asset {key} missing {field}"
            assert data["events"], "l3 layer must carry its operator events"
            for event in data["events"]:
                for field in ("n", "s", "t", "st", "ts", "d", "w", "card"):
                    assert field in event
                card = event["card"]
                for field in ("bound", "bf", "stages", "sp", "klp", "ksp", "raw"):
                    assert field in card
                assert "Duration(us)" in card["raw"], "raw 46-field dump must join kernel_details rows"
        elif key.startswith("timeline/"):
            assert data["schema"] == payloads.TIMELINE_SCHEMA
            for field in ("rank_id", "t0_us", "names", "streams", "op_types", "events", "steps"):
                assert field in data
            assert len(data["events"]) == N_EVENTS
            for row in data["events"]:
                assert len(row) == 6, "timeline event rows are [name, off, dur, stream, op_type, flags]"
            # dictionary coding must round-trip
            first = data["events"][0]
            assert isinstance(data["names"][first[0]], str)
            assert isinstance(data["streams"][first[3]], str)
        elif key == "findings":
            assert data["schema"] == payloads.FINDINGS_SCHEMA
            assert len(data["findings"]) == 2
            assert data["groups"][0]["occurrences"] == 2  # two findings share the rollup key
            assert "evd_step_a0" in data["evidence"]
            assert data["seg_to_class"]["seg_a0"] == CLS_A
        else:  # pragma: no cover - guards future asset kinds
            raise AssertionError(f"unknown asset kind: {key}")


def test_unclassified_steps_get_l2_bucket(built):
    _root, report_dir, _html = built
    manifest = json.loads((report_dir / "assets" / "manifest.json").read_text(encoding="utf-8"))
    key = f"l2/{payloads.UNCLASSIFIED_CLASS_ID}"
    assert key in manifest["assets"], "unclassified steps must keep an L2 view (legacy parity)"
    data = _read_gz(report_dir / manifest["assets"][key]["file"])
    assert [s["seg"] for s in data["steps"]] == ["seg_c0"]
    # unclassified steps route layers to the top-1 class rep's L3
    detail = data["step_detail"]["seg_c0"]
    assert detail["l3_target"]["kind"] == "top1_fallback"
    assert detail["layers"][0]["l3"] is not None


def test_shell_manifest_matches_disk(built):
    _root, report_dir, html = built
    embedded = _boot_var(html, "__ASSET_MANIFEST__")
    disk = json.loads((report_dir / "assets" / "manifest.json").read_text(encoding="utf-8"))
    assert embedded["assets"].keys() == disk["assets"].keys()
    for key, entry in embedded["assets"].items():
        assert entry["file"] == disk["assets"][key]["file"]


def test_l3_links_from_l2_resolve(built):
    """Every L3 reference inside L2 payloads must exist in the manifest."""
    _root, report_dir, _html = built
    manifest = json.loads((report_dir / "assets" / "manifest.json").read_text(encoding="utf-8"))
    for key, entry in manifest["assets"].items():
        if not key.startswith("l2/"):
            continue
        data = _read_gz(report_dir / entry["file"])
        for detail in data["step_detail"].values():
            for layer in detail["layers"]:
                if layer["l3"] is None:
                    continue
                l3_key = f"l3/{layer['l3c']}/{layer['l3']}"
                assert l3_key in manifest["assets"], f"dangling L3 link {l3_key} from {key}"


# ---------------------------------------------------------------------------
# shell contract: detection hints, theme, static L1
# ---------------------------------------------------------------------------


def test_shell_hints_and_theme(built):
    _root, _report_dir, html = built
    # file:// protocol detection + static-server hint
    assert 'location.protocol === "file:"' in html
    assert "python3 -m http.server" in html
    # DecompressionStream feature detection + download fallback
    assert "DecompressionStream" in html
    assert "decomp-banner" in html
    # light theme default + dark toggle + localStorage memory
    assert "ascend-report-theme" in html
    assert 'localStorage' in html
    assert 'data-theme' in html
    assert "theme-toggle" in html
    # fetch-on-demand plumbing + embedded manifest
    assert "__ASSET_MANIFEST__" in html
    assert _boot_var(html, "__EMBEDDED_ASSETS__") is None
    # static L1 sections
    for marker in ("参与 Rank", "EP 峰均比", "DP 陪跑步数", "Layer Validation",
                   "跨 Rank 总览", "算子构成直方图", "Class Rollup",
                   "Findings · rollup 分组", "每 Rank Step 时间线"):
        assert marker in html, f"L1 static section missing: {marker}"
    # first-payload budget
    assert len(html.encode("utf-8")) < 1024 * 1024, "thin shell must stay under 1 MB"


def test_layer_validation_fallback_computed(built):
    """No analysis_summary.json in this fixture → validation is computed from
    segment_manifest + rank_summary with the same shape."""
    _root, _report_dir, html = built
    ov = _boot_var(html, "__OVERVIEW__")
    lv = ov["layer_validation"]
    assert lv["source"] == "computed"
    assert lv["expected_layers"] == 2
    assert lv["detected_layers"]["min"] == 2
    assert lv["layers_match"] is True
    assert lv["status"] == "ok"


def test_analysis_summary_consumed_when_present(tmp_path):
    root = make_root(tmp_path)
    report_dir = root / "report"
    report_dir.mkdir()
    (report_dir / "analysis_summary.json").write_text(json.dumps({
        "schema_version": 1,
        "layer_validation": {
            "status": "degraded", "expected_layers": 61, "expected_source": "config",
            "detected_layers": {"min": 60, "max": 62, "per_rank_outliers": []},
            "layers_match": False, "per_rank_consistent": True,
            "segmentation_mode": "exact_cover", "confidence": "high",
            "limitations": ["synthetic mismatch"],
        },
        "findings": [{
            "finding_type": "device_idle_bubble", "severity": "high",
            "summary": "step 内存在 device idle 空泡", "occurrences": 2,
            "knowledge_refs": [{"id": "kn-1", "title": "bubble 排查手册", "url": "https://example.invalid/kn-1"}],
        }],
    }), encoding="utf-8")
    out = report_dir / "report.html"
    build_html_report_v2(root, out)
    html = out.read_text(encoding="utf-8")
    ov = _boot_var(html, "__OVERVIEW__")
    assert ov["layer_validation"]["source"] == "analysis_summary"
    assert ov["layer_validation"]["status"] == "degraded"
    assert ov["layer_validation"]["expected_layers"] == 61
    # knowledge_refs surfaced in the static L1 section
    assert "Knowledge refs" in html
    assert "bubble 排查手册" in html
    # and attached to the matching findings group in the asset
    findings = _read_gz(report_dir / "assets" / "findings.json.gz")
    assert findings["groups"][0].get("knowledge_refs"), "knowledge_refs must reach the findings asset"


# ---------------------------------------------------------------------------
# single-file mode
# ---------------------------------------------------------------------------


def test_single_file_embeds_all_assets(tmp_path):
    root = make_root(tmp_path)
    out = root / "report" / "report.html"
    build_html_report_v2(root, out, single_file=True)
    html = out.read_text(encoding="utf-8")
    embedded = _boot_var(html, "__EMBEDDED_ASSETS__")
    assert embedded, "single-file mode must embed assets"
    manifest = _boot_var(html, "__ASSET_MANIFEST__")
    assert manifest["single_file"] is True
    # every manifest file is embedded and decodes to the view payload
    files = {entry["file"] for entry in manifest["assets"].values()}
    assert files == set(embedded.keys())
    for name, b64 in embedded.items():
        data = json.loads(gzip.decompress(base64.b64decode(b64)).decode("utf-8"))
        assert data.get("schema"), f"embedded asset {name} missing schema"
    # no assets/ directory is written in single-file mode
    assert not (root / "report" / "assets").exists()


def test_single_file_threshold_refusal(tmp_path):
    root = make_root(tmp_path)
    out = root / "report" / "report.html"
    with pytest.raises(v2_assets.SingleFileTooLargeError, match="assets"):
        build_html_report_v2(root, out, single_file=True, single_file_max_bytes=1)


# ---------------------------------------------------------------------------
# legacy parity + wiring
# ---------------------------------------------------------------------------


def test_legacy_parity_and_unaffected(tmp_path):
    root = make_root(tmp_path)
    v2_out = root / "report" / "report.html"
    build_html_report_v2(root, v2_out)
    legacy_out = root / "legacy.html"
    html_report.build_html_report(root, legacy_out)
    v2_html = v2_out.read_text(encoding="utf-8")
    legacy_html = legacy_out.read_text(encoding="utf-8")

    def kpi_value(html_text: str, label: str) -> str:
        m = re.search(
            re.escape(label) + r".*?<div class=\"value\"[^>]*>([^<]+)</div>",
            html_text, flags=re.S)
        return m.group(1).strip() if m else ""

    # KPI numbers identical
    assert kpi_value(v2_html, "参与 Rank") == kpi_value(legacy_html, "参与 Rank") == "1"
    assert kpi_value(v2_html, "Findings") == kpi_value(legacy_html, "Findings") == "2"
    # step wall average identical
    assert kpi_value(v2_html, "参与 Rank") and "3.00" in v2_html and "3.00" in legacy_html
    # L3 view count parity: legacy sections == v2 l3 assets
    legacy_l3 = len(re.findall(r'class="view" id="view-l3-', legacy_html))
    manifest = json.loads((v2_out.parent / "assets" / "manifest.json").read_text(encoding="utf-8"))
    v2_l3 = len([k for k in manifest["assets"] if k.startswith("l3/")])
    assert v2_l3 == legacy_l3 == 2  # one class, one rep step, two layers
    # legacy renderer remains a working single-file SPA and writes no assets/
    assert 'class="view active" id="view-l1"' in legacy_html
    assert "view-l2-seg_c0" in legacy_html  # legacy covers unclassified steps too
    assert not (root / "assets").exists()


def test_report_parser_renderer_flags():
    parser = report.build_parser()
    args = parser.parse_args(["--output", "x"])
    assert args.html_renderer == "v2"
    assert args.html_single_file is False
    args = parser.parse_args(["--output", "x", "--html-renderer", "legacy", "--html-single-file"])
    assert args.html_renderer == "legacy"
    assert args.html_single_file is True
    with pytest.raises(SystemExit):
        parser.parse_args(["--output", "x", "--html-renderer", "bogus"])


def test_render_report_wires_v2_and_legacy(tmp_path):
    root = make_root(tmp_path / "v2run")
    manifest = report.render_report(root)
    report_dir = root / "report"
    assert manifest["html_status"] == "ok"
    assert manifest["html_renderer"] == "v2"
    assert (report_dir / "report.html").is_file()
    assert (report_dir / "assets" / "manifest.json").is_file()

    root2 = make_root(tmp_path / "legacyrun")
    manifest2 = report.render_report(root2, html_renderer="legacy")
    assert manifest2["html_status"] == "ok"
    assert manifest2["html_renderer"] == "legacy"
    assert manifest2["html_single_file"] is False
    assert (root2 / "report" / "report.html").is_file()
    assert not (root2 / "report" / "assets").exists()

    root3 = make_root(tmp_path / "skiprun")
    manifest3 = report.render_report(root3, skip_html=True)
    assert manifest3["html_status"] == "skipped"
    assert manifest3["html_renderer"] == "v2"  # recorded even when skipped


def test_render_report_v2_single_file_flag(tmp_path):
    root = make_root(tmp_path)
    manifest = report.render_report(root, html_single_file=True)
    assert manifest["html_status"] == "ok"
    assert manifest["html_single_file"] is True
    html = (root / "report" / "report.html").read_text(encoding="utf-8")
    assert _boot_var(html, "__EMBEDDED_ASSETS__"), "flag must embed assets"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
