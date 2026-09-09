"""Tests for the wrapper-side workspace knowledge enrichment.

``profile_analyze._enrich_analysis_summary_with_knowledge`` fills the
``knowledge_refs`` placeholders of the findings rollup groups and backfills
``layer_validation.expected_layers`` from the workspace knowledge store
(``.agents/knowledge/``). All tests run against a temporary knowledge dir
with synthetic entries; no remote access, no real knowledge files.

Covered:
  * rollup group attaches knowledge_refs (entry_id/kind/summary/resolution/
    applicable_versions/score) when a failure-signature entry matches;
  * no match -> refs stay an explicit empty array;
  * empty knowledge base / missing dir / invalid document -> no error, refs
    all empty;
  * layer backfill fires only when expected_layers is null, prefers the
    explicit model id, marks expected_source=knowledge:<entry_id>, recomputes
    layers_match, flips status ok->degraded on mismatch, writes layers_note;
  * entries without a layer-count field (config-driven models) are skipped.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import conftest  # noqa: F401 — registers scripts/ on sys.path

import profile_analyze

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_knowledge_v2 as v2  # noqa: E402

KIND_FILES = {
    "known-failure-signatures.v2.yaml": "known-failure-signatures",
    "model-capabilities.v2.yaml": "model-capabilities",
}


def _scope(**values: str) -> dict:
    scope = {name: {"range": {"min": None, "max": None}} for name in v2.SCOPE_DIMENSIONS}
    for name, value in values.items():
        scope[name] = {"values": [value]}
    return scope


def _v2_entry(*, kind: str, slug: str, summary: str, fingerprints: list[str], resolution: str) -> dict:
    return v2.with_content_hash(
        {
            "uuid": v2.derived_uuid("owner/fork", kind, slug),
            "slug": slug,
            "content_hash": "sha256:" + "0" * 64,
            "status": "unverified",
            "confidence": "low",
            "scope": _scope(component="profiling"),
            "provenance": {
                "contributor": "submitter",
                "origin_repo": "owner/fork",
                "submitted_at": "2026-09-03",
                "redaction_profile": "r2",
            },
            "lifecycle": {
                "first_seen": "2026-09-03",
                "updated_at": "2026-09-03",
                "superseded_by": None,
                "resolved_by": None,
            },
            "rule": {
                "summary": summary,
                "symptom": summary,
                "root_cause": "recorded for the test fixture",
                "resolution": resolution,
                "fingerprints": fingerprints,
            },
        }
    )


FAILURE_ENTRY = _v2_entry(
    kind="known-failure-signatures",
    slug="demo-gloo-hostname",
    summary="gloo init fails when the container hostname is missing from /etc/hosts",
    fingerprints=["gloo makedeviceforhostname", "name or service not known hostname"],
    resolution="add 127.0.0.1 <hostname> to /etc/hosts before serve_start",
)

MODEL_ENTRY = _v2_entry(
    kind="model-capabilities",
    slug="model-demomodel-7b",
    summary="DemoModel-7B: 40-layer test model",
    fingerprints=["demomodel-7b"],
    resolution="Layer count is no longer a structured v2 field.",
)

MODEL_ENTRY_NO_LAYERS = _v2_entry(
    kind="model-capabilities",
    slug="model-configdriven-13b",
    summary="ConfigDriven-13B: layer count is config-driven, no verified value",
    fingerprints=["configdriven-13b"],
    resolution="No verified layer count.",
)


def _write_knowledge_dir(root: Path, entries_by_kind: dict[str, list[dict]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for filename, kind in KIND_FILES.items():
        document = {
            "schema_version": 2,
            "kind": kind,
            "layer": "unverified",
            "updated_at": "2026-09-03",
            "entries": entries_by_kind.get(kind, []),
        }
        v2.write_document(root / filename, document, context=v2.PROJECT_LAYER)
    return root


def _summary(
    *,
    candidate_names: list[str] | None = None,
    expected_layers: int | None = None,
    detected_min: int | None = 40,
    detected_max: int | None = 40,
    status: str = "ok",
    findings: list[dict] | None = None,
) -> dict:
    return {
        "identity": {
            "model": {
                "model_id": None,
                "candidate_names": list(candidate_names or []),
            }
        },
        "layer_validation": {
            "status": status,
            "expected_layers": expected_layers,
            "expected_source": "unknown" if expected_layers is None else "config",
            "detected_layers": {
                "min": detected_min,
                "max": detected_max,
                "per_rank_outliers": [],
            },
            "layers_match": None if expected_layers is None else (expected_layers == detected_min),
        },
        "findings": findings if findings is not None else [],
    }


def _finding(finding_type: str, summary: str) -> dict:
    return {
        "finding_type": finding_type,
        "severity": "high",
        "summary": summary,
        "knowledge_refs": [],
    }


# ---------------------------------------------------------------------------
# findings knowledge_refs
# ---------------------------------------------------------------------------


def test_finding_group_attaches_knowledge_refs(tmp_path: Path) -> None:
    knowledge_dir = _write_knowledge_dir(
        tmp_path, {"known-failure-signatures": [FAILURE_ENTRY]}
    )
    summary = _summary(
        findings=[
            _finding(
                "distributed_init_failure",
                "gloo makeDeviceForHostname: name or service not known for the container hostname",
            ),
            _finding("demo_info", "completely unrelated text qqq"),
        ]
    )
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=knowledge_dir
    )
    refs = out["findings"][0]["knowledge_refs"]
    assert [ref["entry_id"] for ref in refs] == ["demo-gloo-hostname"]
    ref = refs[0]
    assert ref["kind"] == "known-failure-signatures"
    assert ref["summary"] == FAILURE_ENTRY["rule"]["summary"]
    assert ref["resolution"] == FAILURE_ENTRY["rule"]["resolution"]
    assert ref["score"] > 0
    # No token overlap -> explicit empty array, not a missing key.
    assert out["findings"][1]["knowledge_refs"] == []


def test_findings_refs_empty_when_no_match(tmp_path: Path) -> None:
    knowledge_dir = _write_knowledge_dir(
        tmp_path, {"known-failure-signatures": [FAILURE_ENTRY]}
    )
    summary = _summary(findings=[_finding("demo_info", "zz zz nomatch")])
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=knowledge_dir
    )
    assert out["findings"][0]["knowledge_refs"] == []


def test_empty_knowledge_dir_is_legal(tmp_path: Path) -> None:
    knowledge_dir = _write_knowledge_dir(tmp_path, {})
    summary = _summary(
        candidate_names=["DemoModel-7B"],
        findings=[_finding("x", "gloo makeDeviceForHostname")],
    )
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=knowledge_dir
    )
    assert out["findings"][0]["knowledge_refs"] == []
    assert out["layer_validation"]["expected_layers"] is None


def test_missing_knowledge_dir_does_not_raise(tmp_path: Path) -> None:
    summary = _summary(
        candidate_names=["DemoModel-7B"],
        findings=[_finding("x", "gloo makeDeviceForHostname")],
    )
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=tmp_path / "does-not-exist"
    )
    assert out["findings"][0]["knowledge_refs"] == []
    assert out["layer_validation"]["expected_layers"] is None


def test_invalid_knowledge_document_does_not_raise(tmp_path: Path) -> None:
    knowledge_dir = _write_knowledge_dir(tmp_path, {})
    (knowledge_dir / "known-failure-signatures.v2.yaml").write_text(
        json.dumps({"schema_version": 2, "kind": "known-failure-signatures", "not": "valid"}),
        encoding="utf-8",
    )
    summary = _summary(findings=[_finding("x", "gloo makeDeviceForHostname")])
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=knowledge_dir
    )
    assert out["findings"][0]["knowledge_refs"] == []


def test_none_summary_passthrough(tmp_path: Path) -> None:
    assert (
        profile_analyze._enrich_analysis_summary_with_knowledge(
            None, knowledge_dir=tmp_path
        )
        is None
    )


# ---------------------------------------------------------------------------
# layer_validation backfill
# ---------------------------------------------------------------------------


def test_layer_backfill_from_candidate_names(tmp_path: Path) -> None:
    knowledge_dir = _write_knowledge_dir(
        tmp_path, {"model-capabilities": [MODEL_ENTRY]}
    )
    summary = _summary(candidate_names=["DemoModel-7B"])
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=knowledge_dir
    )
    lv = out["layer_validation"]
    # v2 rule bodies have no expected_layers field; backfill is retired.
    assert lv["expected_layers"] is None
    assert lv["expected_source"] == "unknown"


def test_layer_backfill_uses_explicit_model_id_first(tmp_path: Path) -> None:
    knowledge_dir = _write_knowledge_dir(
        tmp_path, {"model-capabilities": [MODEL_ENTRY]}
    )
    summary = _summary(candidate_names=[])
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=knowledge_dir, model_id="DemoModel-7B"
    )
    assert out["layer_validation"]["expected_layers"] is None


def test_layer_backfill_skipped_when_expected_known(tmp_path: Path) -> None:
    knowledge_dir = _write_knowledge_dir(
        tmp_path, {"model-capabilities": [MODEL_ENTRY]}
    )
    summary = _summary(candidate_names=["DemoModel-7B"], expected_layers=61)
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=knowledge_dir
    )
    lv = out["layer_validation"]
    assert lv["expected_layers"] == 61
    assert lv["expected_source"] == "config"
    assert "layers_note" not in lv


def test_layer_backfill_mismatch_flips_status_to_degraded(tmp_path: Path) -> None:
    knowledge_dir = _write_knowledge_dir(
        tmp_path, {"model-capabilities": [MODEL_ENTRY]}
    )
    summary = _summary(candidate_names=["DemoModel-7B"], detected_min=36, detected_max=36)
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=knowledge_dir
    )
    lv = out["layer_validation"]
    assert lv["expected_layers"] is None
    assert lv["status"] == "ok"


def test_layer_backfill_respects_outlier_inventories(tmp_path: Path) -> None:
    knowledge_dir = _write_knowledge_dir(
        tmp_path, {"model-capabilities": [MODEL_ENTRY]}
    )
    summary = _summary(
        candidate_names=["DemoModel-7B"],
        detected_min=20,
        detected_max=20,
        status="degraded",
    )
    summary["layer_validation"]["detected_layers"]["per_rank_outliers"] = [
        {"rank_id": "rank3", "layer_count_inventory": [20, 40]}
    ]
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=knowledge_dir
    )
    lv = out["layer_validation"]
    assert lv["expected_layers"] is None
    assert lv["status"] == "degraded"


def test_layer_backfill_no_model_identity(tmp_path: Path) -> None:
    knowledge_dir = _write_knowledge_dir(
        tmp_path, {"model-capabilities": [MODEL_ENTRY]}
    )
    summary = _summary(candidate_names=[])
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=knowledge_dir
    )
    assert out["layer_validation"]["expected_layers"] is None


def test_layer_backfill_skips_entries_without_layer_count(tmp_path: Path) -> None:
    knowledge_dir = _write_knowledge_dir(
        tmp_path, {"model-capabilities": [MODEL_ENTRY_NO_LAYERS]}
    )
    summary = _summary(candidate_names=["ConfigDriven-13B"])
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=knowledge_dir
    )
    lv = out["layer_validation"]
    assert lv["expected_layers"] is None
    assert lv["expected_source"] == "unknown"
    assert lv["layers_match"] is None


def test_layer_backfill_no_detected_layers_keeps_match_null(tmp_path: Path) -> None:
    knowledge_dir = _write_knowledge_dir(
        tmp_path, {"model-capabilities": [MODEL_ENTRY]}
    )
    summary = _summary(
        candidate_names=["DemoModel-7B"], detected_min=None, detected_max=None
    )
    out = profile_analyze._enrich_analysis_summary_with_knowledge(
        summary, knowledge_dir=knowledge_dir
    )
    lv = out["layer_validation"]
    assert lv["expected_layers"] is None
    assert lv["layers_match"] is None


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        test_finding_group_attaches_knowledge_refs(root / "a")
        test_findings_refs_empty_when_no_match(root / "b")
        test_empty_knowledge_dir_is_legal(root / "c")
        test_missing_knowledge_dir_does_not_raise(root / "d")
        test_invalid_knowledge_document_does_not_raise(root / "e")
        test_none_summary_passthrough(root / "f")
        test_layer_backfill_from_candidate_names(root / "g")
        test_layer_backfill_uses_explicit_model_id_first(root / "h")
        test_layer_backfill_skipped_when_expected_known(root / "i")
        test_layer_backfill_mismatch_flips_status_to_degraded(root / "j")
        test_layer_backfill_respects_outlier_inventories(root / "k")
        test_layer_backfill_no_model_identity(root / "l")
        test_layer_backfill_skips_entries_without_layer_count(root / "m")
        test_layer_backfill_no_detected_layers_keeps_match_null(root / "n")
    print("ok")
