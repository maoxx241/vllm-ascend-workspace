#!/usr/bin/env python3
"""Regression tests for the cross-repository boundary guard.

Two halves. The first runs the shipped policy and baseline against the real
tree, so the recorded violations cannot silently drift. The second builds
throwaway repositories in a temp dir and proves the mechanics: a reverse
dependency is detected, the baseline accepts a known one, a newly introduced
one still fails, and a docstring mention is not mistaken for a dependency.
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / ".agents" / "scripts" / "repo_boundary_check.py"
POLICY = ROOT / ".agents" / "policy" / "repo-boundaries.json"
BASELINE = ROOT / ".agents" / "policy" / "repo-boundaries-baseline.json"
KNOWN_EXTRACTIONS = {"remote-dev", "vaws-coordinator", "vaws-top"}


def load_checker():
    spec = importlib.util.spec_from_file_location("_vaws_repo_boundary_check_test", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = load_checker()


def invoke(*argv: str) -> tuple[int, dict]:
    """Run the checker's argv entry point and return (exit code, payload)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = guard.main(list(argv))
    return code, json.loads(out.getvalue())


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


SYNTHETIC_POLICY = {
    "version": 1,
    "scan": {
        "include_suffixes": [".py"],
        "skip_roots": ["vllm", "vllm-ascend", "__pycache__"],
        "fixture_paths": [".agents/tests/fixture_only.py"],
        "module_search_paths": [".agents/lib", ".remote-dev"],
    },
    "ignored_modules": [{"module": "mcp", "why": "collides with the installed SDK"}],
    "subsystems": [
        {
            "id": "substrate",
            "repo": "org/substrate",
            "layer": 10,
            "roots": [".remote-dev"],
            "public_paths": [".remote-dev", ".remote-dev/tools"],
            "private_paths": [".remote-dev/core", ".remote-dev/state"],
            "public_modules": ["core.result"],
        },
        {
            "id": "dashboard",
            "repo": "org/dashboard",
            "layer": 10,
            "roots": [],
            "inline_patterns": ["dash-branch"],
            "published_external_references": [
                "https://github.com/org/dashboard.git",
                ".agents/skills/dash/SKILL.md",
            ],
        },
        {
            "id": "domain",
            "repo": "org/scaffold",
            "layer": 30,
            "roots": [".agents"],
            "public_paths": [],
            "private_paths": [".agents"],
        },
    ],
    "rules": [
        {"id": "R1", "kind": "layer-direction", "severity": "error", "why": "downward only"},
        {"id": "R2", "kind": "published-entry-point", "severity": "error", "why": "entry points only"},
        {"id": "R3", "kind": "skill-entry-point", "severity": "error", "why": "skills stay separable"},
        {"id": "R4", "kind": "extracted-inline-reference", "severity": "error", "why": "no in-repo branch"},
    ],
}

EMPTY_BASELINE = {"version": 1, "generated_on": "2026-01-01", "note": "test", "accepted": []}


class SyntheticRepo:
    """A throwaway tree shaped like the real one: a substrate, a domain layer
    with two skills, and an upstream submodule that must not be policed."""

    def __init__(self, root: Path) -> None:
        self.root = root
        write(root / ".agents" / "lib" / "domain_helper.py", "VALUE = 1\n")
        write(root / ".remote-dev" / "core" / "__init__.py", "")
        write(root / ".remote-dev" / "core" / "result.py", "def make_result():\n    return {}\n")
        write(root / ".remote-dev" / "core" / "shell_ops.py", "def remote_bash():\n    return 0\n")
        write(root / ".remote-dev" / "tools" / "remote_bash.py", "import core.result\n")
        for name in ("skill-a", "skill-b"):
            write(root / ".agents" / "skills" / name / "SKILL.md", f"---\nname: {name}\n---\n\n# {name}\n")
            write(root / ".agents" / "skills" / name / "scripts" / "entry.py", "PLACEHOLDER = 1\n")
        self.policy_path = root / ".agents" / "policy" / "repo-boundaries.json"
        self.baseline_path = root / ".agents" / "policy" / "repo-boundaries-baseline.json"
        self.set_policy(SYNTHETIC_POLICY)
        self.set_baseline(EMPTY_BASELINE)

    def set_policy(self, payload: dict) -> None:
        write(self.policy_path, json.dumps(payload, indent=2) + "\n")

    def set_baseline(self, payload: dict) -> None:
        write(self.baseline_path, json.dumps(payload, indent=2) + "\n")

    def accept(self, *entries: dict) -> None:
        rows = []
        for entry in entries:
            row = {"removed_by": "substrate", "accepted_on": "2026-01-01", "why": "recorded"}
            row.update(entry)
            rows.append(row)
        self.set_baseline({**EMPTY_BASELINE, "accepted": rows})

    def run(self, *argv: str) -> tuple[int, dict]:
        return invoke("--repo-root", str(self.root), *argv)

    def fingerprints(self, payload: dict, *, key: str = "violations") -> set[str]:
        return {item["fingerprint"] for item in payload[key]}


class RealTreeTests(unittest.TestCase):
    """The shipped policy and baseline must describe the tree as it is."""

    def test_enforce_passes_on_the_current_tree(self) -> None:
        code, payload = invoke("--repo-root", str(ROOT), "--mode", "enforce")
        self.assertEqual(payload["status"], "passed", payload["new_violations"] + payload["stale_baseline"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["counts"]["new"], 0)
        self.assertEqual(payload["baseline"]["stale_count"], 0)
        self.assertEqual(payload["baseline"]["unattributed_count"], 0)

    def test_report_mode_does_not_pretend_the_tree_is_clean(self) -> None:
        code, payload = invoke("--repo-root", str(ROOT), "--mode", "report")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "reported")
        # There are known violations today; a guard that reported zero would be
        # lying, and a guard that exited non-zero here would get switched off.
        self.assertGreater(payload["counts"]["violations"], 0)
        self.assertTrue(all(item["accepted"] for item in payload["violations"]))

    def test_upstream_submodules_are_never_scanned(self) -> None:
        _code, payload = invoke("--repo-root", str(ROOT), "--mode", "report")
        for name in ("vllm", "vllm-ascend"):
            self.assertIn(name, payload["scanned"]["skipped_roots"])
        self.assertFalse([item for item in payload["violations"] if item["path"].startswith(("vllm/", "vllm-ascend/"))])

    def test_every_baseline_row_names_the_extraction_that_removes_it(self) -> None:
        rows = json.loads(BASELINE.read_text(encoding="utf-8"))["accepted"]
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(row=f"{row['path']}:{row['symbol']}"):
                self.assertIn(row["removed_by"], KNOWN_EXTRACTIONS)
                self.assertNotEqual(row["removed_by"], guard.UNATTRIBUTED)
                self.assertTrue(row["why"].strip(), "an accepted violation without a reason rots")
                self.assertRegex(row["accepted_on"], r"^\d{4}-\d{2}-\d{2}$")

    def test_the_only_scan_exemption_is_this_test_file(self) -> None:
        """The exemption exists so the guard can have fixtures; it is not a
        way to hide a real violation, so it must stay this small."""
        policy = guard.load_policy(POLICY, ROOT)
        self.assertEqual(policy.fixture_paths, frozenset({".agents/tests/test_repo_boundary_check.py"}))

    def test_remaining_accepted_rows_are_the_coordinator_extraction(self) -> None:
        rows = json.loads(BASELINE.read_text(encoding="utf-8"))["accepted"]
        self.assertEqual(len(rows), 41)
        self.assertEqual({row["removed_by"] for row in rows}, {"vaws-coordinator"})
        code, payload = invoke("--repo-root", str(ROOT), "--mode", "report")
        self.assertEqual(code, 0)
        self.assertEqual(payload["counts"]["accepted_by_extraction"], {"vaws-coordinator": 41})
        self.assertFalse([item for item in payload["violations"] if item.get("to") == "vaws-top"])

    def test_policy_and_baseline_carry_no_absolute_user_paths(self) -> None:
        for path in (POLICY, BASELINE):
            text = path.read_text(encoding="utf-8")
            for forbidden in ("/Users/", "/home/", "/root/"):
                self.assertNotIn(forbidden, text)


class DetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = SyntheticRepo(Path(self.temp.name))

    def test_reverse_import_from_a_lower_layer_is_a_violation(self) -> None:
        write(
            self.repo.root / ".remote-dev" / "core" / "endpoint.py",
            "import sys\n\n\ndef resolve():\n    sys.path.insert(0, '.agents/lib')\n    import domain_helper\n    return domain_helper.VALUE\n",
        )
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertIn(
            "R1|.remote-dev/core/endpoint.py|domain_helper",
            self.repo.fingerprints(payload, key="new_violations"),
        )
        self.assertEqual(payload["counts"]["by_direction"]["substrate->domain"], 2)

    def test_lazy_import_inside_a_function_is_found(self) -> None:
        """The real reverse dependency hides in a function body, where a textual
        search of the call site shows nothing."""
        write(
            self.repo.root / ".remote-dev" / "core" / "endpoint.py",
            '"""No path literal anywhere near the import."""\n\n\ndef resolve(payload):\n'
            "    if payload:\n"
            "        from domain_helper import VALUE\n"
            "        return VALUE\n"
            "    return None\n",
        )
        _code, payload = self.repo.run("--mode", "report")
        self.assertEqual(
            [(item["kind"], item["symbol"]) for item in payload["violations"]],
            [("import", "domain_helper.VALUE")],
        )

    def test_docstring_and_comment_mentions_are_not_dependencies(self) -> None:
        write(
            self.repo.root / ".remote-dev" / "core" / "ssh_transport.py",
            '"""Shares the mux dir with .agents/lib/domain_helper.py tooling."""\n\n'
            "# See .agents/lib/domain_helper.py for the matching local behaviour.\n\n\n"
            "def connect():\n"
            '    """Historical copies still exist in .agents/lib/domain_helper.py."""\n'
            "    return True\n",
        )
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["counts"]["violations"]), (0, 0))

    def test_a_path_named_inside_a_larger_literal_is_extracted(self) -> None:
        """A non-docstring literal is reported even inside a message or a shell
        command, and the reported symbol is the path, not the subsystem root."""
        write(
            self.repo.root / ".remote-dev" / "core" / "endpoint.py",
            "def resolve():\n    raise RuntimeError('resolution requires .agents/lib/domain_helper.py here')\n",
        )
        _code, payload = self.repo.run("--mode", "report")
        self.assertEqual([item["symbol"] for item in payload["violations"]], [".agents/lib/domain_helper.py"])

    def test_a_fixture_path_is_not_scanned(self) -> None:
        write(self.repo.root / ".agents" / "tests" / "fixture_only.py", "SUBSTRATE = '.remote-dev/core/endpoint.py'\n")
        write(self.repo.root / ".agents" / "tests" / "real.py", "SUBSTRATE = '.remote-dev/core/endpoint.py'\n")
        _code, payload = self.repo.run("--mode", "report")
        self.assertEqual([item["path"] for item in payload["violations"]], [".agents/tests/real.py"])
        self.assertEqual(payload["scanned"]["fixture_paths"], [".agents/tests/fixture_only.py"])

    def test_published_entry_point_is_allowed_and_internals_are_not(self) -> None:
        write(
            self.repo.root / ".agents" / "scripts" / "consume.py",
            "from core.result import make_result\nfrom core.shell_ops import remote_bash\n",
        )
        _code, payload = self.repo.run("--mode", "report")
        symbols = {item["symbol"]: item["rule"] for item in payload["violations"]}
        self.assertNotIn("core.result.make_result", symbols)
        self.assertEqual(symbols.get("core.shell_ops.remote_bash"), "R2")

    def test_private_state_directory_of_another_subsystem_is_reported(self) -> None:
        write(
            self.repo.root / ".agents" / "scripts" / "setup.py",
            "BACKUPS = '.remote-dev/state/client-setup'\nSERVER = '.remote-dev/tools/remote_bash.py'\n",
        )
        _code, payload = self.repo.run("--mode", "report")
        self.assertEqual(
            [(item["rule"], item["symbol"]) for item in payload["violations"]],
            [("R2", ".remote-dev/state/client-setup")],
        )

    def test_skill_may_only_use_a_sibling_skill_s_published_script(self) -> None:
        caller = self.repo.root / ".agents" / "skills" / "skill-a" / "scripts" / "caller.py"
        write(caller, "COMMAND = 'python3 .agents/skills/skill-b/scripts/entry.py sync'\n")
        _code, payload = self.repo.run("--mode", "report")
        self.assertEqual([item["rule"] for item in payload["violations"]], ["R3"])

        # Naming the script in the owning skill's SKILL.md publishes it, which
        # is exactly what an agent routing to that skill can discover.
        skill_md = self.repo.root / ".agents" / "skills" / "skill-b" / "SKILL.md"
        skill_md.write_text(
            skill_md.read_text(encoding="utf-8") + "\n`python3 .agents/skills/skill-b/scripts/entry.py`\n",
            encoding="utf-8",
        )
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["counts"]["violations"]), (0, 0))

    def test_extracted_subsystem_referenced_as_an_in_repo_branch(self) -> None:
        write(
            self.repo.root / ".agents" / "skills" / "skill-a" / "scripts" / "bootstrap.py",
            "DEFAULT_BRANCH = 'dash-branch'\n",
        )
        _code, payload = self.repo.run("--mode", "report")
        self.assertEqual(
            [(item["rule"], item["to"], item["symbol"]) for item in payload["violations"]],
            [("R4", "dashboard", "dash-branch")],
        )

    def test_old_worktree_route_is_detected_and_standalone_refs_are_not(self) -> None:
        write(
            self.repo.root / ".agents" / "skills" / "skill-a" / "scripts" / "old_route.py",
            "DEFAULT_BRANCH = 'dash-branch'\n"
            "COMMAND = ['git', 'worktree', 'add', '/tmp/monitor', 'dash-branch']\n"
            "import subprocess\n"
            "subprocess.run('git worktree add /tmp/monitor dash-branch', shell=True)\n",
        )
        write(
            self.repo.root / ".agents" / "skills" / "skill-a" / "scripts" / "standalone.py",
            "DEFAULT_REPO_URL = 'https://github.com/org/dashboard.git'\n"
            "AGENT_SKILL = '.agents/skills/dash/SKILL.md'\n"
            "SSH_URL = 'git@github.com:org/dashboard.git'\n"
            "JOINED = clone / '.agents/skills/dash/SKILL.md'\n",
        )
        _code, payload = self.repo.run("--mode", "report")
        self.assertEqual(
            {(item["path"], item["symbol"], item["rule"]) for item in payload["violations"]},
            {
                (".agents/skills/skill-a/scripts/old_route.py", "dash-branch", "R4"),
            },
        )

    def test_external_reference_does_not_hide_inrepo_route(self) -> None:
        policy = copy.deepcopy(SYNTHETIC_POLICY)
        top = policy["subsystems"][1]
        top["repo"] = "vllm-ascend-workspace/vaws-top"
        top["inline_patterns"] = ["vaws-top", ".agents/skills/vaws-top/"]
        top["published_external_references"] = [
            ".agents/skills/vaws-top/",
            ".agents/skills/vaws-top/SKILL.md",
        ]
        self.repo.set_policy(policy)
        cases = {
            "shell_branch": "import subprocess\nsubprocess.run('git worktree add /tmp/monitor vaws-top', shell=True)\n",
            "local_skill": "from pathlib import Path\nPath('.agents/skills/vaws-top/SKILL.md').read_text()\n",
            "wrong_host": "ORIGIN = 'https://github.example.invalid/org/vaws-top.git'\n",
            "file_url": "ORIGIN = 'file:///tmp/vaws-top'\n",
            "mixed_literal": (
                "COMMAND = 'https://github.com/vllm-ascend-workspace/vaws-top.git"
                "\\ngit worktree add /tmp/monitor vaws-top'\n"
            ),
        }
        for name, source in cases.items():
            with self.subTest(case=name):
                write(self.repo.root / ".agents" / "skills" / "skill-a" / "scripts" / "edge.py", source)
                _code, payload = self.repo.run("--mode", "report")
                hits = [row for row in payload["violations"] if row["rule"] == "R4"]
                self.assertTrue(hits, (name, payload["violations"]))
        write(
            self.repo.root / ".agents" / "skills" / "skill-a" / "scripts" / "edge.py",
            "DEFAULT_REPO_URL = 'https://github.com/vllm-ascend-workspace/vaws-top.git'\n"
            "AGENT_SKILL = '.agents/skills/vaws-top/SKILL.md'\n"
            "JOINED = clone / '.agents/skills/vaws-top/SKILL.md'\n",
        )
        _code, payload = self.repo.run("--mode", "report")
        self.assertEqual([row for row in payload["violations"] if row["rule"] == "R4"], [])

    def test_scaffold_root_join_is_a_local_read(self) -> None:
        policy = copy.deepcopy(SYNTHETIC_POLICY)
        top = policy["subsystems"][1]
        top["repo"] = "vllm-ascend-workspace/vaws-top"
        top["inline_patterns"] = ["vaws-top", ".agents/skills/vaws-top/"]
        top["published_external_references"] = [
            ".agents/skills/vaws-top/",
            ".agents/skills/vaws-top/SKILL.md",
        ]
        self.repo.set_policy(policy)
        sources = (
            "from pathlib import Path\nROOT = Path(__file__).resolve().parents[4]\n"
            "(ROOT / '.agents/skills/vaws-top/SKILL.md').read_text()\n",
            "from pathlib import Path\n"
            "(Path(__file__).resolve().parents[4] / '.agents/skills/vaws-top/SKILL.md').read_text()\n",
            "from pathlib import Path\nROOT = Path(__file__).resolve().parents[4]\n"
            "open(ROOT / '.agents/skills/vaws-top/SKILL.md')\n",
            "from pathlib import Path\nROOT = Path(__file__).resolve().parents[4]\n"
            "ROOT.joinpath('.agents/skills/vaws-top/SKILL.md').is_file()\n",
        )
        for source in sources:
            with self.subTest(source=source):
                write(self.repo.root / ".agents" / "skills" / "skill-a" / "scripts" / "edge.py", source)
                _code, payload = self.repo.run("--mode", "report")
                hits = [row for row in payload["violations"] if row["rule"] == "R4"]
                self.assertTrue(hits, payload["violations"])

    def test_scaffold_origin_is_retained_across_path_components(self) -> None:
        policy = copy.deepcopy(SYNTHETIC_POLICY)
        top = policy["subsystems"][1]
        top["repo"] = "vllm-ascend-workspace/vaws-top"
        top["inline_patterns"] = ["vaws-top", ".agents/skills/vaws-top/"]
        top["published_external_references"] = [
            ".agents/skills/vaws-top/",
            ".agents/skills/vaws-top/SKILL.md",
        ]
        self.repo.set_policy(policy)
        skill = repr(".agents/skills/vaws-top/SKILL.md")
        roots = ("Path(__file__).resolve().parents[4]", "Path(__file__).absolute().parent")
        shapes = (
            "(ROOT / {p}).read_bytes()",
            "ROOT.joinpath({p}).exists()",
            "open(ROOT / {p}).read()",
            "Path(ROOT, {p}).read_text()",
            'ROOT.joinpath("subdir", {p}).is_file()',
            '(ROOT / "subdir" / {p}).read_text()',
            'ROOT.joinpath("subdir").joinpath({p}).stat()',
        )
        for root in roots:
            for shape in shapes:
                source = "from pathlib import Path\nROOT = " + root + "\n" + shape.format(p=skill) + "\n"
                with self.subTest(root=root, shape=shape):
                    write(self.repo.root / ".agents" / "skills" / "skill-a" / "scripts" / "edge.py", source)
                    _code, payload = self.repo.run("--mode", "report")
                    hits = [row for row in payload["violations"] if row["rule"] == "R4"]
                    self.assertTrue(hits, source)

    def test_external_locator_and_metadata_remain_allowed(self) -> None:
        policy = copy.deepcopy(SYNTHETIC_POLICY)
        top = policy["subsystems"][1]
        top["repo"] = "vllm-ascend-workspace/vaws-top"
        top["inline_patterns"] = ["vaws-top", ".agents/skills/vaws-top/"]
        top["published_external_references"] = [
            ".agents/skills/vaws-top/",
            ".agents/skills/vaws-top/SKILL.md",
        ]
        self.repo.set_policy(policy)
        skill = repr(".agents/skills/vaws-top/SKILL.md")
        sources = (
            "DESCRIPTOR = " + skill + "\n",
            'DESCRIPTOR = {"agent_skill": ' + skill + "}\n",
            "clone = locate_clone()\n(clone / " + skill + ").read_text()\n",
            "clone = locate_clone()\nclone.joinpath(" + skill + ").exists()\n",
        )
        for source in sources:
            with self.subTest(source=source):
                write(self.repo.root / ".agents" / "skills" / "skill-a" / "scripts" / "edge.py", source)
                _code, payload = self.repo.run("--mode", "report")
                hits = [row for row in payload["violations"] if row["rule"] == "R4"]
                self.assertEqual(hits, [])

    def test_upstream_submodule_content_is_skipped(self) -> None:
        write(self.repo.root / "vllm" / "plugin.py", "import domain_helper\nX = '.agents/lib'\n")
        write(self.repo.root / "vllm-ascend" / "plugin.py", "import domain_helper\n")
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["counts"]["violations"]), (0, 0))


class BaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = SyntheticRepo(Path(self.temp.name))
        self.endpoint = self.repo.root / ".remote-dev" / "core" / "endpoint.py"
        write(self.endpoint, "import domain_helper\n")
        self.known = {
            "rule": "R1",
            "path": ".remote-dev/core/endpoint.py",
            "symbol": "domain_helper",
            "removed_by": "substrate",
        }

    def test_baseline_accepts_the_recorded_violation(self) -> None:
        self.repo.accept(self.known)
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["status"]), (0, "passed"))
        self.assertEqual(payload["counts"], {**payload["counts"], "violations": 1, "accepted": 1, "new": 0})
        self.assertEqual(payload["violations"][0]["removed_by"], "substrate")

    def test_a_newly_introduced_violation_still_fails(self) -> None:
        self.repo.accept(self.known)
        write(
            self.repo.root / ".remote-dev" / "core" / "monitor_ops.py",
            "from domain_helper import VALUE\n",
        )
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["status"]), (1, "failed"))
        self.assertEqual(
            self.repo.fingerprints(payload, key="new_violations"),
            {"R1|.remote-dev/core/monitor_ops.py|domain_helper.VALUE"},
        )
        # The accepted one is still accepted; the baseline is not all-or-nothing.
        self.assertEqual(payload["counts"]["accepted"], 1)
        self.assertIn("baseline", payload["next"])

    def test_report_mode_never_fails_even_with_new_violations(self) -> None:
        write(self.repo.root / ".remote-dev" / "core" / "monitor_ops.py", "import domain_helper\n")
        code, payload = self.repo.run("--mode", "report")
        self.assertEqual((code, payload["status"]), (0, "reported"))
        self.assertEqual(payload["counts"]["new"], 2)

    def test_a_baseline_row_that_no_longer_matches_fails(self) -> None:
        """The anti-rot half: an allowlist nobody has to shrink becomes
        invisible, so a fix must delete its row in the same commit."""
        self.repo.accept(self.known, {**self.known, "path": ".remote-dev/core/gone.py"})
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["status"]), (1, "failed"))
        self.assertEqual([row["path"] for row in payload["stale_baseline"]], [".remote-dev/core/gone.py"])
        self.assertEqual(payload["counts"]["new"], 0)

    def test_an_unattributed_baseline_row_fails(self) -> None:
        """`--write-baseline` cannot be used to silence a violation without
        deciding which extraction removes it."""
        self.repo.accept({**self.known, "removed_by": guard.UNATTRIBUTED})
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["status"]), (1, "failed"))
        self.assertEqual(payload["baseline"]["unattributed_count"], 1)
        self.assertEqual(payload["unattributed_baseline"], ["R1|.remote-dev/core/endpoint.py|domain_helper"])

    def test_fingerprints_survive_edits_above_the_violation(self) -> None:
        self.repo.accept(self.known)
        write(self.endpoint, "# unrelated change\n\n\nimport domain_helper\n")
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["violations"][0]["lines"]), (0, [4]))

    def test_write_baseline_preserves_attribution_and_flags_new_rows(self) -> None:
        self.repo.accept(self.known)
        write(self.repo.root / ".remote-dev" / "core" / "monitor_ops.py", "import domain_helper\n")
        code, _payload = self.repo.run("--mode", "report", "--write-baseline", "--today", "2026-02-02")
        self.assertEqual(code, 0)
        rows = {
            row["path"]: row
            for row in json.loads(self.repo.baseline_path.read_text(encoding="utf-8"))["accepted"]
        }
        self.assertEqual(rows[".remote-dev/core/endpoint.py"]["accepted_on"], "2026-01-01")
        self.assertEqual(rows[".remote-dev/core/endpoint.py"]["why"], "recorded")
        self.assertEqual(rows[".remote-dev/core/monitor_ops.py"]["removed_by"], guard.UNATTRIBUTED)
        self.assertEqual(rows[".remote-dev/core/monitor_ops.py"]["accepted_on"], "2026-02-02")
        # Regenerating is not a way to pass.
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["baseline"]["unattributed_count"]), (1, 1))

    def test_write_baseline_drops_rows_whose_violation_is_gone(self) -> None:
        self.repo.accept(self.known, {**self.known, "path": ".remote-dev/core/gone.py"})
        self.repo.run("--mode", "report", "--write-baseline")
        rows = json.loads(self.repo.baseline_path.read_text(encoding="utf-8"))["accepted"]
        self.assertEqual([row["path"] for row in rows], [".remote-dev/core/endpoint.py"])


class PolicyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = SyntheticRepo(Path(self.temp.name))

    def test_shipped_policy_declares_the_three_extracted_subsystems(self) -> None:
        policy = guard.load_policy(POLICY, ROOT)
        repos = {subsystem.repo for subsystem in policy.subsystems}
        self.assertLessEqual(
            {
                "vllm-ascend-workspace/remote-dev",
                "vllm-ascend-workspace/vaws-coordinator",
                "vllm-ascend-workspace/vaws-top",
            },
            repos,
        )
        for rule in policy.rules:
            with self.subTest(rule=rule.id):
                self.assertTrue(rule.why.strip(), "a rule nobody can justify gets deleted, not obeyed")

    def test_scaffold_publishes_nothing_downward(self) -> None:
        policy = guard.load_policy(POLICY, ROOT)
        scaffold = policy.subsystem("scaffold-domain")
        self.assertEqual(scaffold.public_paths, ())
        self.assertEqual(scaffold.public_modules, ())
        self.assertEqual(
            max(subsystem.layer for subsystem in policy.subsystems if subsystem.id != "workspace-root"),
            scaffold.layer,
        )

    def test_module_ownership_follows_the_file_not_the_policy_text(self) -> None:
        policy = guard.load_policy(POLICY, ROOT)
        ownership = guard.Ownership(policy, ROOT)
        for module, expected in (
            ("vaws_task_client", "coordinator"),
            ("vaws_ready_runtime", "coordinator"),
            ("vaws_ssh", "scaffold-domain"),
        ):
            with self.subTest(module=module):
                owner = ownership.subsystem_for_module(module)
                self.assertIsNotNone(owner)
                assert owner is not None
                self.assertEqual(owner.id, expected)
        # `core.*` belonged to the in-tree substrate. It left with the
        # remote-dev extraction, so no file in this tree owns the name any
        # more and the guard must not invent an owner for it. The synthetic
        # repository in DetectionTests still covers derived ownership of an
        # in-tree substrate module.
        self.assertIsNone(ownership.subsystem_for_module("core"))
        # The substrate's `mcp/` package shares a name with the installed MCP
        # SDK that the coordinator imports; owning it would invent a dependency.
        self.assertIsNone(ownership.subsystem_for_module("mcp"))

    def test_unusable_policy_is_blocked_rather_than_crashing(self) -> None:
        for payload in ({"version": 2}, {"version": 1, "subsystems": []}):
            with self.subTest(payload=payload):
                self.repo.set_policy(payload)
                code, result = self.repo.run("--mode", "report")
                self.assertEqual((code, result["status"]), (2, "blocked"))
                self.assertTrue(result["error"])

    def test_missing_baseline_is_blocked_rather_than_treated_as_empty(self) -> None:
        self.repo.baseline_path.unlink()
        code, result = self.repo.run("--mode", "enforce")
        self.assertEqual((code, result["status"]), (2, "blocked"))
        self.assertIn("does not exist", result["error"])


if __name__ == "__main__":
    unittest.main()
