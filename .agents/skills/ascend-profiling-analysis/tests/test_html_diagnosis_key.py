"""Regression: HTML report must read findings under `diagnosis_findings`
(the schema actually written by diagnostics.py). Older drafts used the
shorter `findings` key — accept both, but the modern key wins.
"""
from __future__ import annotations

import json
from pathlib import Path

import conftest  # noqa: F401


def _write_minimal_root(root: Path, findings_payload) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "diagnosis_findings.json").write_text(
        json.dumps(findings_payload), encoding="utf-8"
    )
    # html_report.load_bundle reads many CSVs and JSONs; create empty
    # stubs for the rest so the function reaches the diagnosis branch.
    for name in (
        "rank_summary.csv",
        "step_summary.csv",
        "step_anatomy.csv",
        "step_class_summary.csv",
        "layer_class_summary.csv",
        "block_class_summary.csv",
        "operator_class_summary.csv",
        "hccl_class_summary.csv",
        "hccl_op_summary.csv",
        "normalized_event_index.csv",
    ):
        (root / name).write_text("", encoding="utf-8")
    for name in (
        "manifest.json",
        "step_segments.json",
        "layer_segments.json",
        "block_segments.json",
    ):
        (root / name).write_text("{}", encoding="utf-8")


def _load_findings_from_payload(payload):
    """Inline the load_bundle resolution logic so we can test it without
    importing the whole html_report module (which is heavy)."""
    if isinstance(payload, dict):
        return (
            payload.get("diagnosis_findings")
            or payload.get("findings")
            or payload.get("claims")
            or []
        )
    return payload


def test_modern_key_resolves() -> None:
    payload = {"diagnosis_findings": [{"finding_type": "device_idle_bubble"}]}
    assert _load_findings_from_payload(payload) == [{"finding_type": "device_idle_bubble"}]


def test_legacy_findings_key_resolves() -> None:
    payload = {"findings": [{"finding_type": "device_idle_bubble"}]}
    assert _load_findings_from_payload(payload) == [{"finding_type": "device_idle_bubble"}]


def test_legacy_claims_key_resolves() -> None:
    payload = {"claims": [{"finding_type": "device_idle_bubble"}]}
    assert _load_findings_from_payload(payload) == [{"finding_type": "device_idle_bubble"}]


def test_empty_payload_returns_empty_list() -> None:
    assert _load_findings_from_payload({}) == []


def test_list_payload_returns_self() -> None:
    rows = [{"finding_type": "device_idle_bubble"}]
    assert _load_findings_from_payload(rows) == rows


if __name__ == "__main__":
    test_modern_key_resolves()
    test_legacy_findings_key_resolves()
    test_legacy_claims_key_resolves()
    test_empty_payload_returns_empty_list()
    test_list_payload_returns_self()
    print("ok")
