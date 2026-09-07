#!/usr/bin/env python3
"""Tests for the cross-run experiment ledger."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "vllm-ascend-experiment-ledger"
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_run_manifest import new_manifest, transition_status, write_manifest  # noqa: E402


def load_module():
    name = "_experiment_ledger_test"
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / "experiment_ledger.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ledger = load_module()


def make_run(
    root: Path,
    run_id: str,
    *,
    run_type: str = "performance",
    status: str = "passed",
    commit: str = "aaaa1111",
    container: str = "c-1",
    tp: int = 16,
    identity: bool = True,
) -> Path:
    manifest = new_manifest(
        run_type=run_type,
        run_id=run_id,
        workspace_snapshot={"vllm_ascend_commit": commit, "dirty_files": 0} if identity else None,
        environment={"container": container, "cann": "9.1"} if identity else None,
        model={"path": "/w/m"} if identity else None,
        topology={"tensor_parallel_size": tp, "data_parallel_size": 4} if identity else None,
    )
    # transition_status returns a new manifest rather than mutating in place.
    if status != "planned":
        manifest = transition_status(manifest, "running")
        if status != "running":
            manifest = transition_status(manifest, status)
    path = root / run_type / run_id / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_manifest(path, manifest)
    return path


class IndexTests(unittest.TestCase):
    def test_indexes_runs_across_per_skill_trees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "perf-1")
            make_run(root, "corr-1", run_type="correctness")
            index = ledger.build_index(root)
            self.assertEqual(index["run_count"], 2)
            self.assertEqual(index["by_run_type"], {"correctness": 1, "performance": 1})

    def test_sorts_runs_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for run_id in ("perf-c", "perf-a", "perf-b"):
                make_run(root, run_id)
            ids = [run["run_id"] for run in ledger.build_index(root)["runs"]]
            self.assertEqual(ids, sorted(ids))

    def test_non_conforming_manifest_is_reported_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "perf-1")
            stray = root / "benchmark" / "adhoc" / "manifest.json"
            stray.parent.mkdir(parents=True)
            stray.write_text(json.dumps({"started_at": "x", "results": []}), encoding="utf-8")
            index = ledger.build_index(root)
            self.assertEqual(index["run_count"], 1)
            self.assertEqual(len(index["unindexed"]), 1)
            self.assertIn("adhoc", index["unindexed"][0]["path"])

    def test_unparseable_manifest_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken = root / "debug" / "x" / "manifest.json"
            broken.parent.mkdir(parents=True)
            broken.write_text("{not json", encoding="utf-8")
            index = ledger.build_index(root)
            self.assertEqual(index["run_count"], 0)
            self.assertIn("JSONDecodeError", index["unindexed"][0]["reason"])

    def test_duplicate_run_id_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "perf-1", run_type="performance")
            make_run(root, "perf-1", run_type="debug")
            index = ledger.build_index(root)
            self.assertEqual(index["run_count"], 1)
            self.assertIn("duplicate run_id", index["unindexed"][0]["reason"])

    def test_runs_without_identity_are_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "perf-1")
            make_run(root, "perf-2", identity=False)
            index = ledger.build_index(root)
            self.assertEqual(index["runs_without_identity"], ["perf-2"])

    def test_missing_state_root_raises(self) -> None:
        with self.assertRaises(ledger.LedgerError):
            ledger.build_index(Path("/nonexistent/vaws-ledger-root"))

    def test_terminal_flag_tracks_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "perf-done", status="passed")
            make_run(root, "perf-live", status="running")
            by_id = {run["run_id"]: run for run in ledger.build_index(root)["runs"]}
            self.assertTrue(by_id["perf-done"]["terminal"])
            self.assertFalse(by_id["perf-live"]["terminal"])


class FlattenTests(unittest.TestCase):
    def test_nested_mapping_becomes_dotted_keys(self) -> None:
        flat = ledger.flatten("topology", {"tensor_parallel_size": 16, "nested": {"a": 1}})
        self.assertEqual(
            flat, {"topology.tensor_parallel_size": "16", "topology.nested.a": "1"}
        )

    def test_lists_compare_by_json_form(self) -> None:
        self.assertEqual(ledger.flatten("k", [2, 1]), {"k": "[2, 1]"})

    def test_none_and_empty_string_are_equal(self) -> None:
        self.assertEqual(ledger.flatten("k", None), ledger.flatten("k", ""))


class CompareTests(unittest.TestCase):
    def compare(self, *, vary: list[str], **overrides) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "base")
            make_run(root, "cand", **overrides)
            index = ledger.build_index(root)
            return ledger.compare_runs(
                ledger.select_run(index, "base"),
                ledger.select_run(index, "cand"),
                vary=vary,
            )

    def test_single_declared_difference_is_comparable(self) -> None:
        result = self.compare(commit="bbbb2222", vary=["workspace_snapshot.vllm_ascend_commit"])
        self.assertEqual(result["verdict"], "comparable")
        self.assertEqual(result["confounders"], [])
        self.assertEqual(len(result["intended_differences"]), 1)

    def test_undeclared_difference_is_a_confounder(self) -> None:
        result = self.compare(
            commit="bbbb2222", container="c-2", vary=["workspace_snapshot.vllm_ascend_commit"]
        )
        self.assertEqual(result["verdict"], "not-comparable")
        self.assertEqual([item["key"] for item in result["confounders"]], ["environment.container"])

    def test_topology_change_alongside_code_change_blocks(self) -> None:
        result = self.compare(
            commit="bbbb2222", tp=8, vary=["workspace_snapshot.vllm_ascend_commit"]
        )
        self.assertEqual(result["verdict"], "not-comparable")
        self.assertIn(
            "topology.tensor_parallel_size", [item["key"] for item in result["confounders"]]
        )

    def test_identical_runs_with_no_vary_are_comparable(self) -> None:
        result = self.compare(vary=[])
        self.assertEqual(result["verdict"], "comparable")

    def test_declared_key_that_did_not_move_is_surfaced(self) -> None:
        result = self.compare(vary=["workspace_snapshot.vllm_ascend_commit"])
        self.assertEqual(
            result["declared_but_identical"], ["workspace_snapshot.vllm_ascend_commit"]
        )
        self.assertEqual(result["verdict"], "comparable")

    def test_misspelled_vary_key_blocks(self) -> None:
        result = self.compare(commit="bbbb2222", vary=["workspace_snapshot.typo_commit"])
        self.assertEqual(result["verdict"], "not-comparable")
        self.assertTrue(
            any("absent from both runs" in reason for reason in result["blocking_reasons"])
        )

    def test_non_terminal_run_blocks(self) -> None:
        result = self.compare(status="running", vary=[])
        self.assertEqual(result["verdict"], "not-comparable")
        self.assertTrue(
            any("terminal status" in reason for reason in result["blocking_reasons"])
        )

    def test_missing_identity_blocks(self) -> None:
        result = self.compare(identity=False, vary=[])
        self.assertEqual(result["verdict"], "not-comparable")
        self.assertTrue(
            any("no identity metadata" in reason for reason in result["blocking_reasons"])
        )

    def test_unknown_run_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "base")
            with self.assertRaises(ledger.LedgerError):
                ledger.select_run(ledger.build_index(root), "nope")


class CliTests(unittest.TestCase):
    def test_index_writes_output_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "perf-1")
            output = root / "out" / "index.json"
            code = ledger.main(
                ["index", "--state-root", str(root), "--output", str(output)]
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["run_count"], 1)

    def test_index_filters_do_not_hide_unindexed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "perf-1")
            make_run(root, "corr-1", run_type="correctness")
            stray = root / "stray" / "manifest.json"
            stray.parent.mkdir(parents=True)
            stray.write_text("{}", encoding="utf-8")
            output = root / "index.json"
            ledger.main(
                [
                    "index", "--state-root", str(root),
                    "--run-type", "performance", "--output", str(output),
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["run_count"], 1)
            self.assertEqual(len(payload["unindexed"]), 1)

    def test_show_exits_zero_and_unknown_run_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "perf-1")
            self.assertEqual(
                ledger.main(["show", "--state-root", str(root), "--run-id", "perf-1"]), 0
            )
            self.assertEqual(
                ledger.main(["show", "--state-root", str(root), "--run-id", "nope"]), 2
            )

    def test_compare_exit_code_separates_answer_from_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "base")
            make_run(root, "cand", commit="bbbb2222")
            comparable = ledger.main(
                [
                    "compare", "--state-root", str(root),
                    "--baseline", "base", "--candidate", "cand",
                    "--vary", "workspace_snapshot.vllm_ascend_commit",
                ]
            )
            self.assertEqual(comparable, 0)
            not_comparable = ledger.main(
                [
                    "compare", "--state-root", str(root),
                    "--baseline", "base", "--candidate", "cand",
                ]
            )
            self.assertEqual(not_comparable, 1)
            errored = ledger.main(
                [
                    "compare", "--state-root", str(root),
                    "--baseline", "base", "--candidate", "missing",
                ]
            )
            self.assertEqual(errored, 2)


if __name__ == "__main__":
    unittest.main()
