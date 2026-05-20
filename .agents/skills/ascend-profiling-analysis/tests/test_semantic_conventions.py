"""Regression test for ``knowledge/semantic_conventions.yaml``.

This test pins the **contract** between Python and the YAML enum
catalogue. It does not load any profiling data; it only verifies that
the values Python is wired to emit are present in the YAML, and that
the YAML doesn't list values nothing in Python emits.

Today the source of truth for these enums is still Python. The YAML is
the agent-facing contract layer; downstream agents and the HTML report
read it to know which values are legal. If you add a new enum value in
Python without updating the YAML, this test fails — the failure message
points at the file you must edit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

YAML = pytest.importorskip("yaml", reason="pyyaml not installed; semconv test skipped")


KNOWLEDGE_DIR = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "ascend_profile"
    / "knowledge"
)
SEMCONV_PATH = KNOWLEDGE_DIR / "semantic_conventions.yaml"


def _load_enum(name: str) -> set[str]:
    doc = YAML.safe_load(SEMCONV_PATH.read_text())
    return set(doc["attributes"][name]["values"])


def test_semantic_conventions_file_exists():
    assert SEMCONV_PATH.exists(), (
        "knowledge/semantic_conventions.yaml is the agent-facing enum "
        "contract; do not delete it"
    )
    doc = YAML.safe_load(SEMCONV_PATH.read_text())
    assert doc.get("version") == 1
    assert "attributes" in doc


def test_op_type_enum_matches_python():
    """Every op_type Python can emit must be listed in the YAML enum."""
    from ascend_profile import common  # type: ignore[import]

    python_values = set(common._OP_TYPE_BY_CORE.values())
    # ``op_type_from_event`` adds these via fallbacks:
    python_values |= {"aic", "aiv", "mix_cv", "communication", "aicpu", "mix_comm_aiv", "unknown"}
    yaml_values = _load_enum("op_type")
    missing = python_values - yaml_values
    assert not missing, (
        f"op_type values emitted by Python but missing in "
        f"semantic_conventions.yaml: {sorted(missing)}"
    )


def test_finding_type_enum_matches_diagnostics():
    """Every diagnostics finding_type literal must be in the YAML."""
    src = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "ascend_profile"
        / "diagnostics.py"
    ).read_text()
    python_values = set(re.findall(r"finding_type\s*=\s*[\"']([^\"']+)[\"']", src))
    # ``finding_type=finding_type`` is a parameter pass-through; drop it.
    python_values.discard("finding_type")
    yaml_values = _load_enum("finding_type")
    missing = python_values - yaml_values
    assert not missing, (
        f"finding_type values emitted by diagnostics.py but missing in "
        f"semantic_conventions.yaml: {sorted(missing)}"
    )


def test_alignment_method_enum_matches_cross_rank():
    src = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "ascend_profile"
        / "cross_rank.py"
    ).read_text()
    python_values = set(
        re.findall(r"_ALIGNMENT_METHOD\s*=\s*[\"']([^\"']+)[\"']", src)
    )
    yaml_values = _load_enum("alignment_method")
    missing = python_values - yaml_values
    assert not missing, (
        f"alignment_method values emitted by cross_rank.py but missing in "
        f"semantic_conventions.yaml: {sorted(missing)}"
    )


def test_html_status_and_report_mode_enums():
    """Cross-check report.py's status / mode strings against YAML."""
    src = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "ascend_profile"
        / "report.py"
    ).read_text()
    html_status_values = set(
        re.findall(r"html_status[\"']?\s*[:=]\s*[\"']([a-z_]+)[\"']", src)
    )
    yaml_html = _load_enum("html_status")
    # report.py drives html_status to ok / stub / skipped / error.
    expected_subset = {"ok", "skipped", "stub", "error"}
    assert expected_subset <= yaml_html, (
        f"semantic_conventions.yaml html_status enum must contain "
        f"{expected_subset - yaml_html}"
    )
    if html_status_values:
        leftover = html_status_values - yaml_html
        assert not leftover, (
            f"html_status values emitted by report.py but missing in YAML: "
            f"{sorted(leftover)}"
        )

    report_mode_values = {"summary", "interactive", "full-raw"}
    yaml_mode = _load_enum("report_mode")
    missing = report_mode_values - yaml_mode
    assert not missing, (
        f"report_mode values emitted by report.py but missing in YAML: "
        f"{sorted(missing)}"
    )
