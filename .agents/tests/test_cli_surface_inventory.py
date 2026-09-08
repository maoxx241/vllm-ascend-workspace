#!/usr/bin/env python3
"""Tests for the CLI surface inventory tool."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents" / "scripts" / "cli_surface_inventory.py"
DOCS = ROOT / "docs" / "cli-surface.md"
DISCOVERY_FIXTURE_PATH = ROOT / ".agents" / "tests" / "fixtures" / "cli-surface-inventory.json"
DISCOVERY_FIXTURE = json.loads(DISCOVERY_FIXTURE_PATH.read_text(encoding="utf-8"))


def load_module():
    module_name = "_cli_surface_inventory_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


inventory = load_module()


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def build_fixture(root: Path) -> None:
    """Write the miniature discovery scaffold from the committed JSON fixture."""
    for item in DISCOVERY_FIXTURE["files"]:
        write(root, item["path"], item["text"])


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        build_fixture(self.root)
        self.payload = inventory.build_inventory(self.root, quiet=True)
        self.by_path = {e["path"]: e for e in self.payload["entry_points"]}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_entry_point_definition(self) -> None:
        named = DISCOVERY_FIXTURE["named_paths"]
        self.assertEqual(sorted(self.by_path), DISCOVERY_FIXTURE["expected_entry_points"])
        for path in DISCOVERY_FIXTURE["excluded_paths"]:
            self.assertNotIn(path, self.by_path)
        self.assertEqual(named["thing"], DISCOVERY_FIXTURE["expected_entry_points"][1])

    def test_inline_argparse_verbs_and_helper_options(self) -> None:
        thing = self.by_path[DISCOVERY_FIXTURE["named_paths"]["thing"]]
        self.assertEqual(thing["parser_style"], "argparse")
        self.assertEqual(thing["verbs"], ["create", "status", "remove"])
        self.assertEqual(thing["options"], ["--machine", "--session-id", "--force"])
        self.assertEqual(thing["docstring"], "Manage things.")

    def test_delegated_wrapper_resolves_library_parser(self) -> None:
        probe = self.by_path[DISCOVERY_FIXTURE["named_paths"]["agents_remote_probe"]]
        self.assertEqual(probe["parser_style"], "delegated")
        self.assertEqual(probe["delegate"], "toolbox.cli_probe")
        self.assertEqual(probe["options"], ["--machine", "--timeout"])
        self.assertEqual(probe["docstring"], "Toolbox library.")

    def test_main_tool_dispatcher_filters_branches_by_literal(self) -> None:
        bash = self.by_path[DISCOVERY_FIXTURE["named_paths"]["remote_bash"]]
        self.assertEqual(bash["delegate"], "_cli.main('bash')")
        self.assertIn("--command", bash["options"])
        self.assertEqual(bash["options"].count("--host"), 1)
        self.assertIn("--host", bash["options"])
        self.assertNotIn("--file-path", bash["options"])
        self.assertNotIn("--job-id", bash["options"])

    def test_bare_argv_style(self) -> None:
        self.assertEqual(
            self.by_path[DISCOVERY_FIXTURE["named_paths"]["bare"]]["parser_style"],
            "bare-argv",
        )

    def test_references_are_attributed_to_the_right_collision_sibling(self) -> None:
        named = DISCOVERY_FIXTURE["named_paths"]
        agents_probe = self.by_path[named["agents_remote_probe"]]
        substrate_probe = self.by_path[named["substrate_remote_probe"]]
        self.assertTrue(agents_probe["basename_collision"])
        self.assertEqual([r["path"] for r in agents_probe["references"]], [named["agents_md"]])
        self.assertEqual(agents_probe["reference_kinds"], {"routing": 1})
        self.assertEqual(
            [r["path"] for r in substrate_probe["references"]],
            [named["skill_md"]],
        )
        self.assertEqual(substrate_probe["reference_kinds"], {"skill-doc": 1})

    def test_policy_and_source_map_mentions_are_not_executable_callers(self) -> None:
        thing = self.by_path[DISCOVERY_FIXTURE["named_paths"]["thing"]]
        kinds = thing["reference_kinds"]
        self.assertEqual(kinds.get("policy"), 1)
        self.assertEqual(kinds.get("source-map"), 1)
        self.assertEqual(kinds.get("skill-doc"), 1)

    def test_unclassified_entry_points_fail_the_run(self) -> None:
        self.assertEqual(self.payload["status"], "failed")
        codes = {(f["code"], f["path"]) for f in self.payload["findings"]}
        self.assertIn(("unclassified", DISCOVERY_FIXTURE["named_paths"]["thing"]), codes)
        self.assertIn("stale-classification", {f["code"] for f in self.payload["findings"]})

    def test_counts_by_area_and_style(self) -> None:
        counts = self.payload["counts"]
        self.assertEqual(counts["by_area"], {".agents/scripts": 2, ".agents/skills": 1, ".remote-dev": 2})
        self.assertEqual(counts["by_parser_style"], {"argparse": 1, "bare-argv": 1, "delegated": 3})
        self.assertEqual(counts["argparse_importing_files"], 3)


def _overlay_script(root: Path, rel: str, body: str = "import argparse\nargparse.ArgumentParser()\nif __name__ == '__main__':\n    pass\n") -> None:
    write(root, rel, body)


class RoleAndExternalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._original = inventory.CLASSIFICATION

    def tearDown(self) -> None:
        inventory.CLASSIFICATION = self._original
        self._tmp.cleanup()

    def test_support_roles_are_non_overlapping_and_sum_to_rows(self) -> None:
        _overlay_script(self.root, ".agents/scripts/supported.py")
        _overlay_script(self.root, ".agents/scripts/compat.py")
        _overlay_script(self.root, ".agents/scripts/internal.py")
        _overlay_script(self.root, ".agents/scripts/generated.py")
        _overlay_script(self.root, ".agents/hooks/hook.py", "if __name__ == '__main__':\n    pass\n")
        _overlay_script(self.root, ".agents/scripts/payload.py")
        _overlay_script(self.root, ".agents/scripts/harness.py")
        inventory.CLASSIFICATION = {
            ".agents/scripts/supported.py": inventory._cls("mechanics", "supported", "agent-facing"),
            ".agents/scripts/compat.py": inventory._cls("mechanics", "compatibility", "shim"),
            ".agents/scripts/internal.py": inventory._cls("mechanics", "internal", "debug CLI"),
            ".agents/scripts/generated.py": inventory._cls(
                "mechanics",
                "generated",
                "projection",
                target=".agents/scripts/supported.py",
                target_kind="local-entry",
            ),
            ".agents/hooks/hook.py": inventory._cls("mechanics", "hook", "hook"),
            ".agents/scripts/payload.py": inventory._cls("mechanics", "payload", "remote payload"),
            ".agents/scripts/harness.py": inventory._cls("mechanics", "harness", "harness"),
        }
        payload = inventory.build_inventory(self.root, quiet=True)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["entry_point_count"], 7)
        self.assertEqual(sum(payload["counts"]["by_support_role"].values()), 7)
        self.assertEqual(sum(payload["counts"]["by_responsibility"].values()), 7)
        self.assertEqual(payload["counts"]["by_support_role"]["supported"], 1)
        self.assertEqual(payload["counts"]["supported"], 1)
        self.assertEqual(payload["proposed_surface"]["status"], "historical-unimplemented")

    def test_local_missing_target_differs_from_external_owner(self) -> None:
        _overlay_script(self.root, ".agents/scripts/local.py")
        _overlay_script(self.root, ".agents/scripts/external_ok.py")
        _overlay_script(self.root, ".agents/scripts/drift.py")
        write(
            self.root,
            ".agents/deps/remote-dev.json",
            json.dumps(
                {
                    "name": "remote-dev",
                    "repository": "vllm-ascend-workspace/remote-dev",
                    "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "consumed_surface": {"cli_tools": "tools/remote_*.py"},
                }
            ),
        )
        inventory.CLASSIFICATION = {
            ".agents/scripts/local.py": inventory._cls("mechanics", "supported", "local owner"),
            ".agents/scripts/external_ok.py": inventory._cls(
                "mechanics",
                "compatibility",
                "delegates to an uninspected provider path",
                target="tools/remote_bash.py",
                target_kind="external",
                external_owner="remote-dev",
            ),
            ".agents/scripts/drift.py": inventory._cls(
                "mechanics",
                "compatibility",
                "points at a local file that does not exist",
                target=".agents/scripts/missing.py",
                target_kind="local-entry",
            ),
        }
        payload = inventory.build_inventory(self.root, quiet=True)
        codes = {(f["code"], f["path"]) for f in payload["findings"]}
        self.assertIn(("local-target-missing", ".agents/scripts/drift.py"), codes)
        self.assertNotIn(("local-target-missing", ".agents/scripts/external_ok.py"), codes)
        self.assertNotIn(("unknown-external-owner", ".agents/scripts/external_ok.py"), codes)
        self.assertEqual(payload["external_owners"]["remote-dev"]["source_availability"], "uninspected")
        self.assertEqual(
            payload["external_owners"]["remote-dev"]["commit"],
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )


class ClassificationOverlayTests(unittest.TestCase):
    def test_overlay_roles_and_targets_are_well_formed(self) -> None:
        for path, meta in inventory.CLASSIFICATION.items():
            self.assertIn(meta["responsibility"], inventory.RESPONSIBILITIES, path)
            self.assertIn(meta["support_role"], inventory.SUPPORT_ROLES, path)
            self.assertTrue(meta["note"], path)
            kind = meta.get("target_kind", "self")
            self.assertIn(kind, inventory.TARGET_KINDS, path)
            proposed = meta.get("proposed_target")
            if proposed:
                self.assertTrue(
                    proposed.startswith("vaws ") or proposed in inventory.NON_COMMAND_TARGETS,
                    f"{path}: unexpected proposed_target {proposed!r}",
                )
                if proposed.startswith("vaws "):
                    self.assertNotEqual(proposed, meta["support_role"], path)
            if meta.get("external_owner"):
                self.assertIn(meta["external_owner"], {name for name, _ in inventory.DEP_DECLARATIONS}, path)
            if kind == "local-entry":
                self.assertTrue(meta.get("target"), path)
                self.assertNotEqual(meta["target"], path)

    def test_historical_snapshot_stays_dated_and_separate(self) -> None:
        snap = inventory.HISTORICAL_SNAPSHOT
        self.assertEqual(snap["status"], "historical")
        self.assertEqual(snap["entry_point_count"], 132)
        self.assertEqual(snap["by_category"], {"mechanics": 81, "judgment": 8, "mixed": 8, "redundant": 35})
        self.assertEqual(snap["proposed_agent_command_count"], 13)
        self.assertEqual(len(inventory.HISTORICAL_PROPOSED_COMMANDS), 13)


class RepositoryCoherenceTests(unittest.TestCase):
    """Compare the current tree to the current overlay and delimited table.

    Historical 132/13 figures stay in HISTORICAL_SNAPSHOT and the dated
    document section. They are not an invariant of ordinary new entry points.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = inventory.build_inventory(ROOT, quiet=True)
        cls.doc = DOCS.read_text(encoding="utf-8")

    def test_repository_is_fully_classified(self) -> None:
        self.assertEqual(self.payload["findings"], [])
        self.assertEqual(self.payload["status"], "passed")

    def test_counts_reconcile_to_emitted_rows(self) -> None:
        counts = self.payload["counts"]
        n = self.payload["entry_point_count"]
        self.assertEqual(n, len(self.payload["entry_points"]))
        self.assertEqual(sum(counts["by_support_role"].values()), n)
        self.assertEqual(sum(counts["by_responsibility"].values()), n)
        self.assertEqual(counts["supported"], counts["by_support_role"]["supported"])

    def test_every_current_entry_has_valid_roles(self) -> None:
        for record in self.payload["entry_points"]:
            cls = record["classification"]
            self.assertIsNotNone(cls, record["path"])
            self.assertIn(cls["responsibility"], inventory.RESPONSIBILITIES, record["path"])
            self.assertIn(cls["support_role"], inventory.SUPPORT_ROLES, record["path"])
            self.assertIn(cls["target_kind"], inventory.TARGET_KINDS, record["path"])
            proposed = cls.get("proposed_target")
            if proposed and proposed.startswith("vaws "):
                self.assertNotEqual(cls["support_role"], proposed)

    def test_proposed_surface_is_explicitly_historical(self) -> None:
        proposed = self.payload["proposed_surface"]
        self.assertEqual(proposed["status"], "historical-unimplemented")
        self.assertEqual(proposed["agent_command_count"], 13)
        self.assertEqual(proposed["agent_commands"], list(inventory.HISTORICAL_PROPOSED_COMMANDS))

    def test_external_owners_come_from_committed_pins(self) -> None:
        owners = self.payload["external_owners"]
        by_repository = {meta["repository"]: meta for meta in owners.values()}
        self.assertEqual(
            by_repository["vllm-ascend-workspace/remote-dev"]["commit"],
            "62045af1f76c803ca392ae413b56bcfe290e6450",
        )
        self.assertEqual(
            by_repository["vllm-ascend-workspace/vaws-coordinator"]["commit"],
            "a7d5005a4df6ab8adf5b16a965127e81a30ee3fc",
        )
        self.assertEqual(
            by_repository["vllm-ascend-workspace/vaws-top"]["commit"],
            "e7af28e629e7fd79c47e9b096f1dc1fd94f665ab",
        )
        self.assertEqual(
            by_repository["vllm-ascend-workspace/vaws-knowledge"]["commit"],
            "4208de3ca88f5146472353f23c5f5d216767bf47",
        )
        for meta in owners.values():
            self.assertEqual(meta["source_availability"], "uninspected")
            self.assertTrue(meta["repository"])
        current_paths = {e["path"] for e in self.payload["entry_points"]}
        self.assertNotIn(".remote-dev/tools/remote_bash.py", current_paths)
        self.assertNotIn(".agents/coordinator/server.py", current_paths)

    def test_current_docs_table_matches_inventory(self) -> None:
        rows = inventory.extract_delimited_table(
            self.doc, inventory.CURRENT_TABLE_BEGIN, inventory.CURRENT_TABLE_END
        )
        self.assertEqual(len(rows), self.payload["entry_point_count"])
        generated = [
            line
            for line in inventory.render_markdown(self.payload).splitlines()
            if line.startswith("| `")
        ]
        self.assertEqual(rows, generated)
        current_section = self.doc.split(inventory.CURRENT_TABLE_BEGIN, 1)[1].split(
            inventory.CURRENT_TABLE_END, 1
        )[0]
        for record in self.payload["entry_points"]:
            self.assertIn(f"`{record['path']}`", current_section, record["path"])
        self.assertNotIn("`.remote-dev/tools/remote_bash.py`", current_section)
        self.assertNotIn("`.agents/coordinator/server.py`", current_section)

    def test_historical_docs_table_is_delimited_and_dated(self) -> None:
        rows = inventory.extract_delimited_table(
            self.doc, inventory.HISTORICAL_TABLE_BEGIN, inventory.HISTORICAL_TABLE_END
        )
        self.assertEqual(len(rows), 132)
        historical = self.doc.split(inventory.HISTORICAL_TABLE_BEGIN, 1)[1].split(
            inventory.HISTORICAL_TABLE_END, 1
        )[0]
        self.assertIn("`.remote-dev/tools/remote_bash.py`", historical)
        self.assertIn("`.agents/coordinator/server.py`", historical)
        self.assertIn(inventory.ORIGINAL_PR85, self.doc)
        self.assertIn("mechanics 81", self.doc)


class CliTests(unittest.TestCase):
    def test_json_on_stdout_progress_on_stderr(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = inventory.main(["--repo-root", str(ROOT), "--format", "summary"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "passed")
        self.assertTrue(err.getvalue().startswith("[cli-surface]"))
        self.assertLessEqual(len(err.getvalue().splitlines()), inventory.MAX_PROGRESS_LINES)

    def test_markdown_render_has_one_row_per_current_entry_point(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            inventory.main(["--repo-root", str(ROOT), "--format", "markdown", "--quiet"])
        rows = [line for line in out.getvalue().splitlines() if line.startswith("| `")]
        payload = inventory.build_inventory(ROOT, quiet=True)
        self.assertEqual(len(rows), payload["entry_point_count"])


if __name__ == "__main__":
    unittest.main()
