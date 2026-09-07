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
    """A miniature scaffold: an inline argparse script with verbs, a delegated
    wrapper, a ``main(tool)`` dispatcher pair, a test, a library, and docs."""
    write(
        root,
        ".agents/scripts/thing.py",
        '''
        """Manage things."""
        import argparse

        def add_target_args(parser):
            parser.add_argument("--machine")
            parser.add_argument("--session-id")

        def build_parser():
            parser = argparse.ArgumentParser(description=__doc__)
            add_target_args(parser)
            sub = parser.add_subparsers(dest="action")
            sub.add_parser("create")
            for name in ("status", "remove"):
                child = sub.add_parser(name)
                child.add_argument("--force", action="store_true")
            return parser

        def main():
            build_parser().parse_args()

        if __name__ == "__main__":
            raise SystemExit(main())
        ''',
    )
    write(
        root,
        ".agents/lib/toolbox.py",
        '''
        """Toolbox library."""
        import argparse

        def add_target_args(parser):
            parser.add_argument("--machine")

        def cli_probe(argv=None):
            parser = argparse.ArgumentParser()
            add_target_args(parser)
            parser.add_argument("--timeout", type=float)
            return 0
        ''',
    )
    write(
        root,
        ".agents/scripts/remote_probe.py",
        '''
        from toolbox import cli_probe

        if __name__ == "__main__":
            raise SystemExit(cli_probe())
        ''',
    )
    write(
        root,
        ".remote-dev/tools/_cli.py",
        '''
        import argparse

        def add_endpoint_args(parser):
            parser.add_argument("--host")
            parser.add_argument("--port")

        def build_parser(tool):
            parser = argparse.ArgumentParser()
            add_endpoint_args(parser)
            if tool == "bash":
                parser.add_argument("--command")
            elif tool == "read":
                parser.add_argument("--file-path")
            elif tool in {"job_status", "job_tail"}:
                parser.add_argument("--job-id")
            return parser

        def main(tool):
            build_parser(tool).parse_args()
        ''',
    )
    write(
        root,
        ".remote-dev/tools/remote_bash.py",
        '''
        from _cli import main

        if __name__ == "__main__":
            raise SystemExit(main("bash"))
        ''',
    )
    write(
        root,
        ".remote-dev/tools/remote_probe.py",
        '''
        from _cli import main

        if __name__ == "__main__":
            raise SystemExit(main("probe"))
        ''',
    )
    write(
        root,
        ".agents/skills/demo/scripts/bare.py",
        '''
        import sys

        def main():
            print(sys.argv)

        if __name__ == "__main__":
            main()
        ''',
    )
    write(root, ".agents/skills/demo/scripts/helper.py", "def helper():\n    return 1\n")
    write(root, ".agents/tests/test_thing.py", "if __name__ == '__main__':\n    pass\n")
    write(root, "vllm/setup.py", "if __name__ == '__main__':\n    pass\n")
    write(
        root,
        ".agents/skills/demo/SKILL.md",
        """
        ---
        name: demo
        ---
        Run `python3 .agents/scripts/thing.py create` then `.remote-dev/tools/remote_probe.py`.
        """,
    )
    write(root, "AGENTS.md", "- use `.agents/scripts/remote_probe.py` for probes\n")


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
        self.assertEqual(
            sorted(self.by_path),
            [
                ".agents/scripts/remote_probe.py",
                ".agents/scripts/thing.py",
                ".agents/skills/demo/scripts/bare.py",
                ".remote-dev/tools/remote_bash.py",
                ".remote-dev/tools/remote_probe.py",
            ],
        )
        self.assertNotIn(".agents/tests/test_thing.py", self.by_path)
        self.assertNotIn("vllm/setup.py", self.by_path)
        self.assertNotIn(".agents/lib/toolbox.py", self.by_path)

    def test_inline_argparse_verbs_and_helper_options(self) -> None:
        thing = self.by_path[".agents/scripts/thing.py"]
        self.assertEqual(thing["parser_style"], "argparse")
        self.assertEqual(thing["verbs"], ["create", "status", "remove"])
        self.assertEqual(thing["options"], ["--machine", "--session-id", "--force"])
        self.assertEqual(thing["docstring"], "Manage things.")

    def test_delegated_wrapper_resolves_library_parser(self) -> None:
        probe = self.by_path[".agents/scripts/remote_probe.py"]
        self.assertEqual(probe["parser_style"], "delegated")
        self.assertEqual(probe["delegate"], "toolbox.cli_probe")
        self.assertEqual(probe["options"], ["--machine", "--timeout"])
        self.assertEqual(probe["docstring"], "Toolbox library.")

    def test_main_tool_dispatcher_filters_branches_by_literal(self) -> None:
        bash = self.by_path[".remote-dev/tools/remote_bash.py"]
        self.assertEqual(bash["delegate"], "_cli.main('bash')")
        self.assertIn("--command", bash["options"])
        self.assertIn("--host", bash["options"])
        self.assertNotIn("--file-path", bash["options"])
        self.assertNotIn("--job-id", bash["options"])

    def test_bare_argv_style(self) -> None:
        self.assertEqual(self.by_path[".agents/skills/demo/scripts/bare.py"]["parser_style"], "bare-argv")

    def test_references_are_attributed_to_the_right_collision_sibling(self) -> None:
        agents_probe = self.by_path[".agents/scripts/remote_probe.py"]
        substrate_probe = self.by_path[".remote-dev/tools/remote_probe.py"]
        self.assertTrue(agents_probe["basename_collision"])
        self.assertEqual([r["path"] for r in agents_probe["references"]], ["AGENTS.md"])
        self.assertEqual(agents_probe["reference_kinds"], {"routing": 1})
        self.assertEqual(
            [r["path"] for r in substrate_probe["references"]],
            [".agents/skills/demo/SKILL.md"],
        )
        self.assertEqual(substrate_probe["reference_kinds"], {"skill-doc": 1})

    def test_unclassified_entry_points_fail_the_run(self) -> None:
        self.assertEqual(self.payload["status"], "failed")
        codes = {(f["code"], f["path"]) for f in self.payload["findings"]}
        self.assertIn(("unclassified", ".agents/scripts/thing.py"), codes)
        # Real-repo classification paths are absent from the fixture.
        self.assertIn("stale-classification", {f["code"] for f in self.payload["findings"]})

    def test_counts_by_area_and_style(self) -> None:
        counts = self.payload["counts"]
        self.assertEqual(counts["by_area"], {".agents/scripts": 2, ".agents/skills": 1, ".remote-dev": 2})
        self.assertEqual(counts["by_parser_style"], {"argparse": 1, "bare-argv": 1, "delegated": 3})
        self.assertEqual(counts["argparse_importing_files"], 3)


class ClassificationOverlayTests(unittest.TestCase):
    def test_overlay_categories_and_targets_are_well_formed(self) -> None:
        for path, meta in inventory.CLASSIFICATION.items():
            self.assertIn(meta["category"], inventory.CATEGORIES, path)
            self.assertTrue(meta["note"], path)
            target = meta["target"]
            if meta["category"] == "redundant":
                self.assertIn(target, inventory.CLASSIFICATION, f"{path}: redundant target must itself be an entry point")
                self.assertNotEqual(inventory.CLASSIFICATION[target]["category"], "redundant", f"{path}: redundant target must not be redundant itself")
            elif meta["category"] == "judgment":
                self.assertEqual(target, "guidance", path)
            else:
                self.assertTrue(
                    target.startswith("vaws ") or target in inventory.NON_COMMAND_TARGETS,
                    f"{path}: unexpected target {target!r}",
                )

    def test_every_judgment_script_runs_nothing_remotely(self) -> None:
        """The judgment classification rests on the claim that these scripts
        never touch the remote host; hold that claim as a test. Local git is
        allowed (change_validation collects the diff itself)."""
        for path, meta in inventory.CLASSIFICATION.items():
            if meta["category"] != "judgment":
                continue
            source = (ROOT / path).read_text(encoding="utf-8")
            for token in ("vaws_ssh", "ssh_exec", "remote_bash", "vaws_remote_toolbox", '"ssh"', "'ssh'"):
                self.assertNotIn(token, source, f"{path} reaches a remote host; reclassify")


class RepositoryStateTests(unittest.TestCase):
    """Hold the current repository to the documented figures.

    When these fail, a script was added, removed, or moved: update
    ``CLASSIFICATION`` and the tables in ``docs/cli-surface.md`` together.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = inventory.build_inventory(ROOT, quiet=True)

    def test_repository_is_fully_classified(self) -> None:
        self.assertEqual(self.payload["findings"], [])
        self.assertEqual(self.payload["status"], "passed")

    def test_documented_counts(self) -> None:
        counts = self.payload["counts"]
        self.assertEqual(self.payload["entry_point_count"], 132)
        self.assertEqual(
            counts["by_category"],
            {"mechanics": 81, "judgment": 8, "mixed": 8, "redundant": 35},
        )
        self.assertEqual(self.payload["target_surface"]["agent_command_count"], 13)
        self.assertEqual(counts["agent_facing_today"], 114)

    def test_docs_table_matches_inventory(self) -> None:
        doc = (ROOT / "docs" / "cli-surface.md").read_text(encoding="utf-8")
        for record in self.payload["entry_points"]:
            self.assertIn(f"`{record['path']}`", doc, f"{record['path']} missing from docs/cli-surface.md")
        for command in self.payload["target_surface"]["agent_commands"]:
            self.assertIn(f"`{command}`", doc, f"{command} missing from docs/cli-surface.md")

    def test_unreferenced_entry_points_are_the_documented_dark_surface(self) -> None:
        """Entry points nothing names outside tests/mirrors. The remote-dev CLI
        fallbacks are only ever mentioned as a directory (`.remote-dev/tools/`)
        because agents are routed to the MCP tools; the design document treats
        that as evidence, so the list is held here rather than hidden."""
        self.assertEqual(
            self.payload["unreferenced_outside_tests"],
            [
                ".agents/scripts/cli_surface_inventory.py",
                ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report_v2/__main__.py",
                ".remote-dev/tools/remote_apply_patch.py",
                ".remote-dev/tools/remote_bash.py",
                ".remote-dev/tools/remote_context_snapshot.py",
                ".remote-dev/tools/remote_edit.py",
                ".remote-dev/tools/remote_glob.py",
                ".remote-dev/tools/remote_grep.py",
                ".remote-dev/tools/remote_ls.py",
                ".remote-dev/tools/remote_monitor.py",
                ".remote-dev/tools/remote_multi_edit.py",
                ".remote-dev/tools/remote_read.py",
                ".remote-dev/tools/remote_write.py",
            ],
        )


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

    def test_markdown_render_has_one_row_per_entry_point(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            inventory.main(["--repo-root", str(ROOT), "--format", "markdown", "--quiet"])
        rows = [line for line in out.getvalue().splitlines() if line.startswith("| `")]
        self.assertEqual(len(rows), 132)


if __name__ == "__main__":
    unittest.main()
