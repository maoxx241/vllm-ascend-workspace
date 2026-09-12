"""Report existing validation evidence without inventing a test plan."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from vaws_coordinator.run_manifest import add_artifact, new_manifest, transition_status, write_manifest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / ".agents/skills/vllm-ascend-change-validation/scripts/change_validation.py"
spec = importlib.util.spec_from_file_location("change_validation_test", SCRIPT)
assert spec and spec.loader
change = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = change
spec.loader.exec_module(change)


@pytest.fixture(autouse=True)
def report_code():
    # Report semantics do not require a snapshot of the developer's worktree.
    with patch("vaws_coordinator.code_identity.manifest_code", return_value={
        "source_head": "1" * 40, "snapshot_commit": "2" * 40, "dirty": True,
    }):
        yield


def diff(path):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"


def existing_run(root, *, comparison=True, certificate=None, parent="another-task"):
    manifest = new_manifest(run_type="correctness", run_id="existing-case", parent_run_id=parent, workspace_root=ROOT)
    if comparison:
        artifact = root / "comparison.json"
        artifact.write_text(json.dumps({"status": "passed", "cases": []}), encoding="utf-8")
        manifest = add_artifact(manifest, name="comparison", kind="comparison", uri=str(artifact))
    if certificate is not None:
        artifact = root / "certificate.json"
        artifact.write_text(json.dumps(certificate), encoding="utf-8")
        manifest = add_artifact(manifest, name="comparability-certificate", kind="comparability-certificate", uri=str(artifact))
    manifest = transition_status(transition_status(manifest, "running"), "passed")
    path = root / "existing.json"
    write_manifest(path, manifest)
    return path


def test_document_change_creates_no_implicit_npu_requirement(tmp_path):
    report = change.build_report(diff_text=diff("docs/graph-kernel-note.md"), baseline="base", candidate="next", output_dir=tmp_path / "report")
    summary = json.loads(Path(report["summary"]).read_text(encoding="utf-8"))
    assert summary["runs"] == []
    assert report["evidence_status"] == "none"
    assert report["status"] == "inconclusive"
    assert not (tmp_path / "report/validation-plan.json").exists()
    assert "targeted-smoke" not in Path(report["report"]).read_text(encoding="utf-8")


def test_unrelated_passed_run_is_retained_without_proving_change(tmp_path):
    child = existing_run(tmp_path)
    before = child.read_bytes()
    report = change.build_report(diff_text=diff("graph_mode.py"), baseline="base", candidate="next", evidence=[child], output_dir=tmp_path / "report")
    summary = json.loads(Path(report["summary"]).read_text(encoding="utf-8"))
    assert summary["runs"][0]["status"] == "passed"
    assert summary["runs"][0]["revision_match"] == "unknown"
    assert report["status"] == "inconclusive"
    assert report["evidence_status"] == "incomplete"
    assert child.read_bytes() == before


def test_missing_artifact_is_visible_without_losing_report(tmp_path):
    child = existing_run(tmp_path)
    (tmp_path / "comparison.json").unlink()
    report = change.build_report(diff_text=diff("worker.py"), baseline="base", candidate="next", evidence=[child], output_dir=tmp_path / "report")
    summary = json.loads(Path(report["summary"]).read_text(encoding="utf-8"))
    assert summary["runs"][0]["artifacts"][0]["available"] is False
    assert "artifact unavailable: comparison" in summary["runs"][0]["limitations"]
    assert report["status"] == "inconclusive"
    assert report["evidence_status"] == "incomplete"


def test_missing_revision_stays_unknown_and_keeps_certificate_reason(tmp_path):
    from vaws_comparability import identity_from_recorded_observation, issue_certificate
    baseline = {"workspace_snapshot": {"vllm_ascend_commit": "base-sha"}, "environment": {"cann": "test"}, "model": {"path": "/models/test"}, "topology": {"tp": 1}}
    candidate = {"environment": {"cann": "changed"}, "model": {"path": "/models/test"}, "topology": {"tp": 1}}
    certificate = issue_certificate(identity_from_recorded_observation("base", baseline), identity_from_recorded_observation("next", candidate))
    record = change.summarize_evidence(existing_run(tmp_path, certificate=certificate), baseline="base-sha", candidate="next-sha")
    assert record["revision_match"] == "unknown"
    assert record["observed_scope"]["baseline"]["workspace_snapshot.vllm_ascend_commit"] == "base-sha"
    assert certificate["blocking_reasons"]
    assert all(any(reason in limitation for limitation in record["limitations"]) for reason in certificate["blocking_reasons"])
    assert any("missing for at least one side" in reason for reason in record["limitations"])


def test_recorded_revision_pair_is_reused_without_parent_reassociation(tmp_path):
    from vaws_comparability import identity_from_recorded_observation, issue_certificate
    def identity(revision):
        return {"workspace_snapshot": {"vllm_ascend_commit": revision}, "environment": {"cann": "test"}, "model": {"path": "/models/test"}, "topology": {"tp": 1}}
    certificate = issue_certificate(identity_from_recorded_observation("base", identity("base-sha")), identity_from_recorded_observation("next", identity("next-sha")), vary=["workspace_snapshot.vllm_ascend_commit"])
    child = existing_run(tmp_path, certificate=certificate)
    record = change.summarize_evidence(child, baseline="base-sha", candidate="next-sha")
    assert record["revision_match"] == "matched"
    assert record["limitations"] == []
    assert change.summarize_evidence(child, baseline="wrong-sha", candidate="next-sha")["revision_match"] == "mismatched"


def test_bare_passed_status_cannot_create_verified_coverage(tmp_path):
    record = change.summarize_evidence(existing_run(tmp_path, comparison=False), baseline="base", candidate="next")
    assert record["revision_match"] == "unknown"
    assert "the run links no artifacts" in record["limitations"]


def test_diff_summary_keeps_changed_paths_and_counts():
    summary = change.parse_diff(diff("docs/note.md") + diff("vllm_ascend/worker.py"))
    assert summary["file_count"] == 2
    assert summary["additions"] == summary["deletions"] == 2


def test_empty_diff_is_an_input_error(tmp_path):
    with pytest.raises(change.ChangeValidationError, match="no changed files"):
        change.build_report(diff_text="", baseline="base", candidate="next", output_dir=tmp_path / "report")
