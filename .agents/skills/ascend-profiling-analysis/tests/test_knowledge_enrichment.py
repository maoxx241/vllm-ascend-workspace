"""Markdown findings references preserve model config and never infer layer counts.

Uses the installed memory backend with temporary Markdown files. Real
OpenViking indexing is exercised separately by the package integration.
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

import pytest


@pytest.fixture(autouse=True)
def isolated_knowledge_backend(monkeypatch):
    monkeypatch.setenv("VAWS_KNOWLEDGE_BACKEND", "memory")


FAILURE_ENTRY = {
    "slug": "demo-gloo-hostname",
    "title": "Gloo hostname resolution failure",
    "content": "gloo makeDeviceForHostname: name or service not known for the container hostname. Check the hostname mapping in /etc/hosts before serving.",
}
MODEL_ENTRY = {"slug": "model-demomodel-7b", "title": "DemoModel-7B", "content": "A report mentions 40 layers. Inspect the actual model config before applying this observation."}
MODEL_ENTRY_NO_LAYERS = {"slug": "model-configdriven-13b", "title": "ConfigDriven-13B", "content": "Layer count is config-driven; no measured value is recorded."}


def _write_knowledge_dir(root: Path, entries_by_kind: dict[str, list[dict]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for entries in entries_by_kind.values():
        for entry in entries:
            (root / (entry['slug'] + '.md')).write_text(
                '# ' + entry['title'] + '\n\n' + entry['content'] + '\n', encoding='utf-8')
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
    assert len(refs) == 1
    assert refs[0]["ref"].endswith("demo-gloo-hostname.md")
    ref = refs[0]
    assert ref["title"] == FAILURE_ENTRY["title"]
    assert "makeDeviceForHostname" in ref["excerpt"]
    assert ref["layer"] == "project"
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
    # Reference prose does not establish the actual model layer count.
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
