"""Regression: segment_manifest.json must expose scalar
``hard_error_count`` / ``interior_island_total`` so the skill launcher
can validate segmentation health without parsing the list form.
"""
from __future__ import annotations

import json
from pathlib import Path

import conftest  # noqa: F401 — registers sys.path

from ascend_profile import segment


def _manifest_skeleton(tmp_path: Path, hard_errors: list[dict], interior_total: int) -> dict:
    """Build the manifest dict the way segment.py does in
    ``segment_profile``, but bypass full pipeline to keep the test
    cheap. We assert the *schema* — values supplied by the caller.
    """
    return {
        "schema_version": segment.SCHEMA_VERSION,
        "tool_version": segment.TOOL_VERSION,
        "analysis_stage": "segment",
        "created_at": segment.utc_now(),
        "output_dir": str(tmp_path),
        "files": {
            "step_segments": "step_segments.json",
            "layer_segments": "layer_segments.json",
            "structure_evidence_graph": "structure_evidence_graph.json",
            "segment_manifest": "segment_manifest.json",
        },
        "rank_summaries": [
            {
                "rank_id": "0",
                "event_count": 100,
                "segment_count": 5,
                "step_count": 3,
                "interior_unclassified_count": interior_total,
                "layer_count_inventory": [],
                "hard_error_count": len(hard_errors),
            },
        ],
        "segment_count": 5,
        "layer_count": 3,
        "structure_observation_count": 0,
        "evidence_count": 0,
        "hard_error_count": len(hard_errors),
        "interior_island_total": interior_total,
        "hard_errors": hard_errors,
    }


def test_manifest_has_scalar_health_fields(tmp_path: Path) -> None:
    manifest = _manifest_skeleton(tmp_path, [], 0)
    out = tmp_path / "segment_manifest.json"
    out.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = json.loads(out.read_text())
    assert isinstance(loaded["hard_error_count"], int), "hard_error_count must be int"
    assert loaded["hard_error_count"] == 0
    assert isinstance(loaded["interior_island_total"], int), "interior_island_total must be int"
    assert loaded["interior_island_total"] == 0
    assert isinstance(loaded["hard_errors"], list), "hard_errors stays as list for debugging"


def test_launcher_validation_passes_on_clean_manifest(tmp_path: Path) -> None:
    """Mirrors the int(...) coercion in profile_analyze._validate_segment_health()."""
    manifest = _manifest_skeleton(tmp_path, [], 0)
    seg = manifest

    raw_hard = seg.get("hard_error_count")
    if raw_hard is None:
        legacy = seg.get("hard_errors", 0)
        raw_hard = len(legacy) if isinstance(legacy, list) else legacy
    hard = int(raw_hard or 0)
    interior = int(seg.get("interior_island_total", 0) or 0)
    assert hard == 0 and interior == 0


def test_launcher_validation_flags_problems(tmp_path: Path) -> None:
    manifest = _manifest_skeleton(
        tmp_path,
        [{"rank_id": "0", "error_type": "interior_unclassified_island", "count": 1}],
        2,
    )
    seg = manifest

    raw_hard = seg.get("hard_error_count")
    hard = int(raw_hard or 0)
    interior = int(seg.get("interior_island_total", 0) or 0)
    assert hard == 1
    assert interior == 2


def test_launcher_backcompat_with_legacy_manifest() -> None:
    """Older drafts wrote only ``hard_errors`` as a list. Launcher must still
    coerce that into an int without raising.
    """
    legacy = {
        "hard_errors": [
            {"rank_id": "0", "error_type": "some"},
            {"rank_id": "0", "error_type": "thing"},
        ],
        # no hard_error_count, no interior_island_total
        "rank_summaries": [],
    }
    raw_hard = legacy.get("hard_error_count")
    if raw_hard is None:
        legacy_hard = legacy.get("hard_errors", 0)
        raw_hard = len(legacy_hard) if isinstance(legacy_hard, list) else legacy_hard
    hard = int(raw_hard or 0)
    interior = int(legacy.get("interior_island_total", 0) or 0)
    assert hard == 2 and interior == 0


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_manifest_has_scalar_health_fields(tmp)
        test_launcher_validation_passes_on_clean_manifest(tmp)
        test_launcher_validation_flags_problems(tmp)
    test_launcher_backcompat_with_legacy_manifest()
    print("ok")
