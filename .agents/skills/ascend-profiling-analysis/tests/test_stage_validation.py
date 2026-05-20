"""Verify the wrapper's stage-aware artifact validation contract.

The wrapper used to demand the full ``REQUIRED_SINGLE_ARTIFACTS`` set
unconditionally, which broke ``--only-stage normalize`` because that
stage doesn't write ``report/report.md``. ``REQUIRED_ARTIFACTS_BY_END_STAGE``
holds the smaller-than-full sets keyed by end stage; this test pins that
contract.
"""
from __future__ import annotations

import conftest  # noqa: F401

import _common as common
import profile_analyze


def test_required_artifacts_table_covers_all_stages() -> None:
    for stage in (
        "normalize",
        "segment",
        "classify",
        "summarize",
        "cross_rank",
        "diagnostics",
        "report",
    ):
        assert stage in common.REQUIRED_ARTIFACTS_BY_END_STAGE, (
            f"REQUIRED_ARTIFACTS_BY_END_STAGE missing key: {stage}"
        )
        files = common.REQUIRED_ARTIFACTS_BY_END_STAGE[stage]
        assert files, f"stage {stage} maps to empty artifact set"
        # ``manifest.json`` is always written and is the wrapper's entry
        # point into the run metadata.
        assert "manifest.json" in files, (
            f"stage {stage} must include manifest.json"
        )


def test_only_stage_normalize_does_not_require_report() -> None:
    """Regression: ``--only-stage normalize`` must not be flagged for the
    absence of ``report/report.md``."""
    files = common.REQUIRED_ARTIFACTS_BY_END_STAGE["normalize"]
    assert "report/report.md" not in files
    assert "report/report.html" not in files


def test_report_stage_artifacts_match_full_set() -> None:
    """End stage ``report`` must still demand the full original set."""
    assert (
        common.REQUIRED_ARTIFACTS_BY_END_STAGE["report"]
        == common.REQUIRED_SINGLE_ARTIFACTS
    )


def test_resolve_end_stage_default_is_report() -> None:
    """Without any stage selector, validation falls back to the full set."""
    assert profile_analyze._resolve_end_stage(None, None, None) == "report"


def test_resolve_end_stage_prefers_only_then_to() -> None:
    assert (
        profile_analyze._resolve_end_stage("segment", None, "report")
        == "segment"
    )
    assert (
        profile_analyze._resolve_end_stage(None, "normalize", "classify")
        == "classify"
    )


def test_required_artifacts_for_unknown_stage_falls_back_to_full() -> None:
    """Unknown stage strings shouldn't silently degrade to nothing — the
    wrapper should ask for the full set so the caller can debug."""
    assert (
        profile_analyze._required_artifacts_for("unknown-stage")
        == common.REQUIRED_SINGLE_ARTIFACTS
    )


if __name__ == "__main__":
    test_required_artifacts_table_covers_all_stages()
    test_only_stage_normalize_does_not_require_report()
    test_report_stage_artifacts_match_full_set()
    test_resolve_end_stage_default_is_report()
    test_resolve_end_stage_prefers_only_then_to()
    test_required_artifacts_for_unknown_stage_falls_back_to_full()
    print("ok")
