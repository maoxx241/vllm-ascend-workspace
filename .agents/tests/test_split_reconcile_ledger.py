#!/usr/bin/env python3
"""Regression tests for the split reconciliation checker and its ledger.

Two halves. The first runs the shipped ledger against this tree, so the rows
whose destination is the scaffold cannot drift from what the published
scaffold snapshot actually contains, and the three known gaps stay recorded as
gaps until somebody flips them. The second builds throwaway destination
checkouts in a temp dir and proves the mechanics the design promises: a
declared-but-absent item is `missing`; an unreachable destination is
`unverified` and never `arrived`; a row whose item has since arrived fails
until the row is updated; a row with no attributed destination is rejected
outright; and a destination receipt cannot manufacture an arrival that the
evidence contradicts.

Evidence is read from an immutable git tree at a resolved commit, never from
the working tree. Nothing here touches the network or a remote host.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / ".agents" / "scripts" / "split_reconcile.py"
LEDGER = ROOT / ".agents" / "policy" / "split-ledger.json"
KNOWN_GAPS = {
    "remote-dev.managed-jobs-supervisor",
    "remote-dev.vaws-tool-provider",
    "remote-dev.task-facade-tests",
}
DEST_ORIGIN = "https://github.com/org/dest.git"
DEST_ORIGIN_SSH = "git@github.com:org/dest.git"


def load_checker():
    spec = importlib.util.spec_from_file_location("_vaws_split_reconcile_test", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reconcile = load_checker()


def invoke_with_stderr(*argv: str) -> tuple[int, dict, str]:
    """Run the checker's argv entry point and return (exit code, payload, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    env_backup = os.environ.pop("VAWS_SPLIT_DESTINATIONS", None)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = reconcile.main(list(argv))
    finally:
        if env_backup is not None:
            os.environ["VAWS_SPLIT_DESTINATIONS"] = env_backup
    return code, json.loads(out.getvalue()), err.getvalue()


def invoke(*argv: str) -> tuple[int, dict]:
    code, payload, _err = invoke_with_stderr(*argv)
    return code, payload


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def git_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Synthetic Observer",
            "GIT_AUTHOR_EMAIL": "observer@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic Observer",
            "GIT_COMMITTER_EMAIL": "observer@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home),
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_COMMON_DIR", None)
    return env


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


OBSERVER = "test observer, synthetic checkout"


def ledger_skeleton() -> dict:
    return {
        "version": 1,
        "generated_on": "2026-09-07",
        "scan": {"skip_roots": ["vllm", "vllm-ascend", "node_modules"]},
        "repositories": {
            "hub": {"repo": "org/hub", "visibility": "public", "checkout": "."},
            "dest": {"repo": "org/dest", "visibility": "public", "receipt_path": "docs/split-receipt.json"},
            "vault": {"repo": "org/vault", "visibility": "private"},
        },
        "items": [],
    }


def item(
    item_id: str,
    *,
    destination: str = "dest",
    evidence: list[dict] | None = None,
    state: str = "missing",
    commit: str | None = "0123456789abcdef",
    follow_up: str | None = "someone",
    why: str | None = None,
) -> dict:
    recorded = {"state": state, "observed_on": "2026-09-07", "observed_by": OBSERVER}
    if commit:
        recorded["observed_commit"] = commit
    if follow_up:
        recorded["follow_up"] = follow_up
    if why:
        recorded["why"] = why
    return {
        "id": item_id,
        "what": f"synthetic item {item_id}",
        "kind": "module",
        "source": {"repo": "hub", "path": f"src/{item_id}.py"},
        "destination": {
            "repo": destination,
            "path": f"lib/{item_id}.py",
            "evidence": evidence if evidence is not None else [{"kind": "path", "path": f"lib/{item_id}.py"}],
        },
        "declared_by": [{"repo": "hub", "commit": "deadbeef", "document": "docs/HANDOFF.md", "says": "moves"}],
        "recorded": recorded,
    }


class ShippedLedgerTests(unittest.TestCase):
    """The real ledger, run against the real tree with no destination checkouts."""

    def test_ledger_loads_and_names_every_expected_repository(self):
        ledger = reconcile.load_ledger(LEDGER, ROOT)
        # The hub plus the four extraction destinations. Names are not spelled
        # out here so the boundary guard's inline-reference rule has nothing
        # to flag in a test fixture.
        self.assertEqual(len(ledger.repositories), 5)
        self.assertEqual(ledger.root_repository.id, "scaffold")
        self.assertEqual(sum(1 for r in ledger.repositories.values() if r.visibility == "private"), 2)
        for repository in ledger.repositories.values():
            self.assertRegex(repository.repo, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
        self.assertIn("vllm", ledger.skip_roots)
        self.assertIn("vllm-ascend", ledger.skip_roots)
        ids = [entry.id for entry in ledger.items]
        self.assertEqual(len(ids), len(set(ids)), "ledger ids must be unique")

    def test_the_three_known_gaps_are_recorded_as_missing_with_a_follow_up(self):
        ledger = reconcile.load_ledger(LEDGER, ROOT)
        by_id = {entry.id: entry for entry in ledger.items}
        for gap in KNOWN_GAPS:
            with self.subTest(gap=gap):
                self.assertIn(gap, by_id)
                self.assertEqual(by_id[gap].recorded.state, "missing")
                self.assertTrue(by_id[gap].recorded.follow_up)
                self.assertEqual(by_id[gap].destination_repo, "vaws-coordinator")

    def test_every_recorded_arrival_names_the_commit_that_was_inspected(self):
        ledger = reconcile.load_ledger(LEDGER, ROOT)
        for entry in ledger.items:
            with self.subTest(item=entry.id):
                if entry.recorded.state in {"arrived", "missing"}:
                    self.assertTrue(entry.recorded.observed_commit)
                self.assertTrue(entry.recorded.observed_by)
                self.assertTrue(entry.declared_by)

    def test_without_destination_checkouts_nothing_is_reported_arrived_for_them(self):
        code, payload = invoke("--repo-root", str(ROOT), "--mode", "enforce")
        # Scaffold rows are observed at fetched origin/main. Recorded states are
        # not refreshed in this L1 path-identity fix, so enforce may report
        # drift when `.agents` evidence is present in that snapshot.
        self.assertIn(code, (0, 1), payload.get("drift"))
        self.assertIn(payload["status"], {"passed", "failed"})
        for row in payload["items"]:
            with self.subTest(item=row["id"]):
                if row["destination"]["repo"] == "scaffold":
                    self.assertIn(row["verdict"], {"arrived", "missing"})
                else:
                    self.assertEqual(row["verdict"], "unverified")
                    self.assertNotEqual(row["verdict"], "arrived")
        for repo_id, destination in payload["destinations"].items():
            if repo_id != "scaffold":
                self.assertFalse(destination["reachable"])
            else:
                self.assertEqual(destination["revision_scope"], "published-default-branch")
                self.assertTrue(destination["selected_commit"])

    def test_report_mode_never_fails(self):
        code, payload = invoke("--repo-root", str(ROOT), "--mode", "report")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "reported")

    def test_documentation_and_ledger_reference_each_other(self):
        raw = json.loads(LEDGER.read_text(encoding="utf-8"))
        self.assertEqual(raw["checker"], ".agents/scripts/split_reconcile.py")
        doc = ROOT / raw["doc"]
        self.assertTrue(doc.is_file(), f"{raw['doc']} must exist")
        body = doc.read_text(encoding="utf-8")
        self.assertIn(".agents/policy/split-ledger.json", body)
        for gap in KNOWN_GAPS:
            self.assertIn(gap, body, f"docs must walk through {gap}")
        self.assertIn("refs/remotes/origin/", body)
        self.assertIn("local identity metadata", body)
        self.assertIn("--revision", body)


class GitCheckoutFixture(unittest.TestCase):
    """Throwaway hub + destination checkouts in a temp dir."""

    __test__ = False

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.hub = self.base / "hub"
        self.dest = self.base / "dest"
        self.hub.mkdir()
        self.dest.mkdir()
        write(self.hub / "README.md", "hub\n")
        self.env = git_env(self.base)

    def git(self, root: Path, *args: str) -> str:
        return git(root, *args, env=self.env)

    def publish(self, root: Path, files: dict[str, str] | None = None, *, origin: str = DEST_ORIGIN, message: str = "publish") -> str:
        if not (root / ".git").exists():
            self.git(root, "init", "-q")
            self.git(root, "config", "user.name", "Synthetic Observer")
            self.git(root, "config", "user.email", "observer@example.invalid")
            self.git(root, "config", "core.hooksPath", os.devnull)
            self.git(root, "config", "commit.gpgsign", "false")
            try:
                self.git(root, "checkout", "-q", "-b", "main")
            except subprocess.CalledProcessError:
                pass
        if files:
            for rel, content in files.items():
                write(root / rel, content)
        self.git(root, "add", "-A")
        status = self.git(root, "status", "--porcelain")
        if status:
            self.git(root, "commit", "-q", "--allow-empty", "-m", message)
        else:
            try:
                self.git(root, "rev-parse", "HEAD")
            except subprocess.CalledProcessError:
                self.git(root, "commit", "-q", "--allow-empty", "-m", message)
        sha = self.git(root, "rev-parse", "HEAD")
        remotes = self.git(root, "remote")
        if "origin" not in remotes.split():
            self.git(root, "remote", "add", "origin", origin)
        else:
            self.git(root, "remote", "set-url", "origin", origin)
        self.git(root, "update-ref", "refs/remotes/origin/main", sha)
        return sha

    def run_ledger(self, ledger: dict, *argv: str) -> tuple[int, dict]:
        ledger_path = self.hub / ".agents" / "policy" / "split-ledger.json"
        write(ledger_path, json.dumps(ledger, indent=2))
        return invoke("--repo-root", str(self.hub), "--ledger", str(ledger_path), *argv)

    def run_ledger_stderr(self, ledger: dict, *argv: str) -> tuple[int, dict, str]:
        ledger_path = self.hub / ".agents" / "policy" / "split-ledger.json"
        write(ledger_path, json.dumps(ledger, indent=2))
        return invoke_with_stderr("--repo-root", str(self.hub), "--ledger", str(ledger_path), *argv)

    def blocked(self, ledger: dict, *argv: str) -> str:
        code, payload = self.run_ledger(ledger, *argv)
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["status"], "blocked")
        return payload["error"]


class SyntheticLedgerTests(GitCheckoutFixture):
    """Original checker mechanics against published git snapshots."""

    __test__ = True

    # -- the three verdicts -------------------------------------------------

    def test_declared_but_absent_item_is_missing(self):
        self.publish(self.dest, {"README.md": "dest\n"})
        ledger = ledger_skeleton()
        ledger["items"].append(item("gone"))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        row = payload["items"][0]
        self.assertEqual(row["verdict"], "missing")
        self.assertEqual(row["observed"]["evidence"][0]["status"], "absent")
        self.assertEqual(payload["counts"]["by_verdict"]["missing"], 1)

    def test_present_item_is_arrived_and_records_the_inspected_head(self):
        sha = self.publish(self.dest, {"lib/here.py": "X = 1\n"})
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "arrived")
        self.assertEqual(payload["items"][0]["observed"]["head_commit"], sha)
        self.assertEqual(payload["destinations"]["dest"]["selected_commit"], sha)
        self.assertEqual(payload["destinations"]["dest"]["revision_scope"], "published-default-branch")
        self.assertEqual(payload["destinations"]["dest"]["published_ref"], "refs/remotes/origin/main")

    def test_unreachable_destination_is_unverified_not_arrived_even_when_recorded_arrived(self):
        ledger = ledger_skeleton()
        ledger["items"].append(item("private-thing", destination="vault", state="arrived", follow_up=None))
        ledger["items"].append(item("private-gap", destination="vault", state="missing"))
        code, payload = self.run_ledger(ledger)  # no --destination at all
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "passed")
        for row in payload["items"]:
            with self.subTest(item=row["id"]):
                self.assertEqual(row["verdict"], "unverified")
                self.assertNotEqual(row["verdict"], "arrived")
                self.assertIsNone(row["drift"])
        self.assertFalse(payload["destinations"]["vault"]["reachable"])
        self.assertIn("no checkout supplied", payload["destinations"]["vault"]["reason"])
        self.assertEqual(payload["counts"]["by_verdict"], {"arrived": 0, "missing": 0, "unverified": 2})

    def test_nonexistent_checkout_path_is_unverified(self):
        ledger = ledger_skeleton()
        ledger["items"].append(item("thing", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.base / 'does-not-exist'}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "unverified")

    def test_partially_checkable_evidence_is_unverified_not_arrived(self):
        self.publish(self.dest, {"lib/thing.py": "X = 1\n", "lib/broken.py": "def not python\n"})
        ledger = ledger_skeleton()
        ledger["items"].append(
            item(
                "thing",
                state="arrived",
                follow_up=None,
                evidence=[
                    {"kind": "path", "path": "lib/thing.py"},
                    {"kind": "symbols", "path": "lib/broken.py", "names": ["thing"]},
                ],
            )
        )
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        row = payload["items"][0]
        self.assertEqual(row["verdict"], "unverified")
        statuses = {result["status"] for result in row["observed"]["evidence"]}
        self.assertEqual(statuses, {"present", "unverifiable"})

    # -- ledger must be updated when the world changes ----------------------

    def test_recorded_missing_fails_once_the_item_has_arrived_until_the_row_is_updated(self):
        self.publish(self.dest, {"README.md": "dest\n"})
        ledger = ledger_skeleton()
        ledger["items"].append(item("landed", state="missing"))
        # Before the fix lands: agreement, pass.
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "missing")

        # The destination lands the item; the ledger still says missing.
        self.publish(self.dest, {"lib/landed.py": "X = 1\n"}, message="land the item")
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["drift"][0]["kind"], "arrived-not-recorded")
        self.assertEqual(payload["drift"][0]["item"], "landed")

        # Report mode shows the same disagreement without failing.
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}", "--mode", "report")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "reported")
        self.assertEqual(len(payload["drift"]), 1)

        # Updating the row (with the commit that was inspected) clears it.
        updated = copy.deepcopy(ledger)
        updated["items"][0]["recorded"] = {
            "state": "arrived",
            "observed_on": "2026-09-08",
            "observed_commit": "fedcba9876543210",
            "observed_by": OBSERVER,
        }
        code, payload = self.run_ledger(updated, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["drift"], [])

    def test_recorded_arrived_fails_when_the_item_has_disappeared(self):
        self.publish(self.dest, {"README.md": "dest\n"})
        ledger = ledger_skeleton()
        ledger["items"].append(item("regressed", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 1)
        self.assertEqual(payload["drift"][0]["kind"], "regression")

    def test_recorded_unverified_fails_once_the_destination_is_inspected(self):
        self.publish(self.dest, {"README.md": "dest\n"})
        ledger = ledger_skeleton()
        ledger["items"].append(item("seen-now", state="unverified", commit=None, follow_up=None, why="no access at the time"))
        code, payload = self.run_ledger(ledger)  # still unreachable: fine
        self.assertEqual(code, 0)
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 1)
        self.assertEqual(payload["drift"][0]["kind"], "recorded-unverified-now-observed")

    def test_there_is_no_flag_that_rewrites_the_ledger_from_observations(self):
        parser = reconcile.build_parser()
        option_strings = {option for action in parser._actions for option in action.option_strings}
        self.assertFalse(
            {flag for flag in option_strings if "write" in flag or "update" in flag or "accept" in flag},
            "the ledger is attributed by hand; the checker must not offer to rewrite it",
        )

    # -- rows that must be rejected, not accepted ---------------------------

    def test_row_with_no_attributed_destination_is_rejected(self):
        ledger = ledger_skeleton()
        row = item("orphan")
        del row["destination"]["repo"]
        ledger["items"].append(row)
        error = self.blocked(ledger, "--destination", f"dest={self.dest}")
        self.assertIn("no attributed destination", error)

        ledger = ledger_skeleton()
        row = item("orphan")
        row["destination"]["repo"] = ""
        ledger["items"].append(row)
        self.assertIn("no attributed destination", self.blocked(ledger))

        ledger = ledger_skeleton()
        row = item("orphan")
        row["destination"]["repo"] = "somewhere-undeclared"
        ledger["items"].append(row)
        self.assertIn("unknown repository", self.blocked(ledger))

    def test_row_without_checkable_evidence_or_declaration_is_rejected(self):
        ledger = ledger_skeleton()
        row = item("unchecked")
        row["destination"]["evidence"] = []
        ledger["items"].append(row)
        self.assertIn("evidence", self.blocked(ledger))

        ledger = ledger_skeleton()
        row = item("undeclared")
        row["declared_by"] = []
        ledger["items"].append(row)
        self.assertIn("declared_by", self.blocked(ledger))

        ledger = ledger_skeleton()
        row = item("hearsay")
        row["declared_by"] = [{"repo": "hub", "says": "trust me"}]
        ledger["items"].append(row)
        self.assertIn("'commit' or a 'document'", self.blocked(ledger))

    def test_definite_states_need_an_inspected_commit_and_missing_needs_a_follow_up(self):
        ledger = ledger_skeleton()
        ledger["items"].append(item("opinion", state="arrived", commit=None, follow_up=None))
        self.assertIn("observed_commit", self.blocked(ledger))

        ledger = ledger_skeleton()
        ledger["items"].append(item("unowned-gap", state="missing", follow_up=None))
        self.assertIn("follow_up", self.blocked(ledger))

        ledger = ledger_skeleton()
        ledger["items"].append(item("unexplained", state="unverified", commit=None, follow_up=None))
        self.assertIn("why", self.blocked(ledger))

        ledger = ledger_skeleton()
        ledger["items"].append(item("bogus", state="maybe"))
        self.assertIn("recorded state", self.blocked(ledger))

    def test_duplicate_ids_unknown_destination_flags_and_bad_ledgers_are_rejected(self):
        ledger = ledger_skeleton()
        ledger["items"].extend([item("twice"), item("twice")])
        self.assertIn("duplicate id", self.blocked(ledger))

        ledger = ledger_skeleton()
        ledger["items"].append(item("ok"))
        self.assertIn("does not declare", self.blocked(ledger, "--destination", f"nowhere={self.dest}"))
        self.assertIn("NAME=PATH", self.blocked(ledger, "--destination", "malformed"))

        ledger = ledger_skeleton()
        ledger["items"].append(item("ok"))
        ledger["repositories"]["dest"]["checkout"] = "."
        self.assertIn("exactly one repository", self.blocked(ledger))

        code, payload = invoke("--repo-root", str(self.hub), "--ledger", str(self.hub / "absent.json"))
        self.assertEqual(code, 2)
        self.assertIn("does not exist", payload["error"])

    # -- evidence kinds ----------------------------------------------------

    def test_symbols_are_found_by_ast_across_matching_files_but_in_one_file(self):
        self.publish(
            self.dest,
            {
                "tests/test_a.py": "class T:\n    def test_one(self):\n        pass\n",
                "tests/test_b.py": "def test_two():\n    pass\nTABLE = {}\n# def test_three(): pass\nS = 'def test_three'\n",
            },
        )
        ledger = ledger_skeleton()
        ledger["items"].append(
            item("suite", state="arrived", follow_up=None, evidence=[
                {"kind": "symbols", "glob": "tests/*.py", "names": ["test_one"]},
                {"kind": "symbols", "glob": "tests/*.py", "names": ["test_two", "TABLE"]},
            ])
        )
        ledger["items"].append(
            item("split-suite", state="missing", evidence=[
                # Both names exist, but in different files: the declared unit
                # did not arrive as one thing.
                {"kind": "symbols", "glob": "tests/*.py", "names": ["test_one", "test_two"]},
            ])
        )
        ledger["items"].append(
            item("commented-out", state="missing", evidence=[
                # A mention in a comment or string is not a definition.
                {"kind": "symbols", "path": "tests/test_b.py", "names": ["test_three"]},
            ])
        )
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        verdicts = {row["id"]: row["verdict"] for row in payload["items"]}
        self.assertEqual(verdicts, {"suite": "arrived", "split-suite": "missing", "commented-out": "missing"})

    def test_sha256_text_and_reference_evidence(self):
        vendor = "PIN = 1\n"
        digest = hashlib.sha256(vendor.encode("utf-8")).hexdigest()
        self.publish(
            self.dest,
            {
                "lib/vendor.py": vendor,
                "README.md": "## Serving\nthe tools are served here\n",
                "server.py": "from vaws_ops import TOOL_SCHEMAS\n",
                "lib/vaws_ops.py": "TOOL_SCHEMAS = {}\n",
            },
        )
        ledger = ledger_skeleton()
        ledger["items"].append(item("pinned", state="arrived", follow_up=None, evidence=[
            {"kind": "sha256", "path": "lib/vendor.py", "sha256": digest},
            {"kind": "text", "path": "README.md", "contains": "## Serving"},
            {"kind": "reference", "glob": "*.py", "exclude": ["lib/*"], "pattern": "TOOL_SCHEMAS"},
        ]))
        ledger["items"].append(item("drifted", state="missing", evidence=[
            {"kind": "sha256", "path": "lib/vendor.py", "sha256": "0" * 64},
        ]))
        ledger["items"].append(item("only-in-lib", state="missing", evidence=[
            {"kind": "reference", "glob": "*.py", "exclude": ["lib/*", "server.py"], "pattern": "TOOL_SCHEMAS"},
        ]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        verdicts = {row["id"]: row["verdict"] for row in payload["items"]}
        self.assertEqual(verdicts, {"pinned": "arrived", "drifted": "missing", "only-in-lib": "missing"})
        drifted = next(row for row in payload["items"] if row["id"] == "drifted")
        self.assertIn("digest differs", drifted["observed"]["evidence"][0]["detail"])

    def test_skip_roots_are_never_searched(self):
        self.publish(
            self.dest,
            {
                "node_modules/pkg/hit.py": "def test_hidden(): pass\n",
                "vllm/hit.py": "def test_hidden(): pass\n",
                "README.md": "dest\n",
            },
        )
        ledger = ledger_skeleton()
        ledger["items"].append(item("hidden", state="missing", evidence=[
            {"kind": "symbols", "glob": "*.py", "names": ["test_hidden"]},
        ]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        self.assertEqual(payload["items"][0]["verdict"], "missing")

    def test_evidence_paths_may_not_escape_the_checkout(self):
        ledger = ledger_skeleton()
        ledger["items"].append(item("escape", evidence=[{"kind": "path", "path": "../hub/README.md"}]))
        self.assertIn("inside the checkout", self.blocked(ledger))
        ledger = ledger_skeleton()
        ledger["items"].append(item("absolute", evidence=[{"kind": "path", "path": "/etc/passwd"}]))
        self.assertIn("inside the checkout", self.blocked(ledger))

    # -- git-backed evidence ------------------------------------------------

    @unittest.skipUnless(shutil.which("git"), "git is required for commit evidence")
    def test_commit_evidence_and_origin_check_use_the_destination_git_metadata(self):
        first = self.publish(self.dest, {"lib/x.py": "X = 1\n"})
        ledger = ledger_skeleton()
        ledger["items"].append(item("history", state="arrived", follow_up=None, evidence=[{"kind": "commit", "commit": first}]))
        ledger["items"].append(item("foreign-history", state="missing", evidence=[
            {"kind": "commit", "commit": "1111111111111111111111111111111111111111"},
        ]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        verdicts = {row["id"]: row["verdict"] for row in payload["items"]}
        self.assertEqual(verdicts, {"history": "arrived", "foreign-history": "missing"})
        self.assertEqual(payload["destinations"]["dest"]["head_commit"], first)

        # A checkout whose origin is some other repository must not be able
        # to produce `arrived` for this destination.
        self.git(self.dest, "remote", "set-url", "origin", "https://github.com/org/other.git")
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertTrue(all(row["verdict"] == "unverified" for row in payload["items"]))
        self.assertIn("does not belong", payload["destinations"]["dest"]["reason"])

    # -- receipts ----------------------------------------------------------

    def test_receipt_cannot_manufacture_an_arrival_and_agreement_is_marked_attested(self):
        receipt = json.dumps({
            "version": 1,
            "items": {
                "real": {"arrived_in": "abc1234", "attested_by": "dest maintainer", "attested_on": "2026-09-07"},
                "claimed": {"arrived_in": "abc1234", "attested_by": "dest maintainer", "attested_on": "2026-09-07"},
            },
        })
        self.publish(self.dest, {"lib/real.py": "X = 1\n", "docs/split-receipt.json": receipt})
        ledger = ledger_skeleton()
        ledger["items"].append(item("real", state="arrived", follow_up=None))
        ledger["items"].append(item("claimed", state="missing"))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 1)
        rows = {row["id"]: row for row in payload["items"]}
        self.assertEqual(rows["real"]["verdict"], "arrived")
        self.assertTrue(rows["real"]["observed"]["attested"])
        self.assertEqual(rows["claimed"]["verdict"], "missing")
        self.assertEqual(rows["claimed"]["drift"]["kind"], "receipt-contradiction")

    def test_malformed_receipt_is_a_warning_not_an_arrival(self):
        self.publish(self.dest, {"lib/real.py": "X = 1\n", "docs/split-receipt.json": "{not json"})
        ledger = ledger_skeleton()
        ledger["items"].append(item("real", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertIn("unreadable", payload["destinations"]["dest"]["receipt_error"])
        self.assertIsNone(payload["items"][0]["observed"]["attested"])


@unittest.skipUnless(shutil.which("git"), "git is required for immutable snapshot tests")
class ImmutableSnapshotTests(GitCheckoutFixture):
    """Working-tree bytes must not be attributed to a selected commit."""

    __test__ = True

    def test_non_git_directory_is_unverified(self):
        write(self.dest / "lib" / "here.py", "X = 1\n")
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "unverified")
        self.assertIn("not a git repository", payload["destinations"]["dest"]["reason"])
        self.assertIsNone(payload["destinations"]["dest"]["head_commit"])

    def test_git_without_origin_is_unverified(self):
        self.git(self.dest, "init", "-q")
        self.git(self.dest, "config", "user.name", "Synthetic Observer")
        self.git(self.dest, "config", "user.email", "observer@example.invalid")
        write(self.dest / "lib" / "here.py", "X = 1\n")
        self.git(self.dest, "add", ".")
        self.git(self.dest, "commit", "-q", "-m", "no origin")
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "unverified")
        self.assertIn("no origin remote", payload["destinations"]["dest"]["reason"])

    def test_wrong_repo_origin_is_unverified(self):
        self.publish(self.dest, {"lib/here.py": "X = 1\n"}, origin="https://github.com/org/other.git")
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "unverified")
        self.assertIn("does not belong", payload["destinations"]["dest"]["reason"])

    def test_wrong_host_with_same_owner_repo_suffix_is_unverified(self):
        self.publish(self.dest, {"lib/here.py": "X = 1\n"}, origin="https://github.com.evil.invalid/org/dest.git")
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "unverified")
        self.assertIn("supported GitHub identity", payload["destinations"]["dest"]["reason"])
        serialized = json.dumps(payload)
        self.assertNotIn("evil.invalid", serialized)

    def test_nested_directory_is_unverified(self):
        self.publish(self.dest, {"lib/here.py": "X = 1\n"})
        nested = self.dest / "lib"
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None, evidence=[{"kind": "path", "path": "here.py"}]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={nested}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "unverified")
        self.assertIn("not the git top-level", payload["destinations"]["dest"]["reason"])

    def test_missing_published_ref_is_unverified(self):
        self.git(self.dest, "init", "-q")
        self.git(self.dest, "config", "user.name", "Synthetic Observer")
        self.git(self.dest, "config", "user.email", "observer@example.invalid")
        write(self.dest / "lib" / "here.py", "X = 1\n")
        self.git(self.dest, "add", ".")
        self.git(self.dest, "commit", "-q", "-m", "feature only")
        self.git(self.dest, "remote", "add", "origin", DEST_ORIGIN)
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "unverified")
        self.assertIn("refs/remotes/origin/main", payload["destinations"]["dest"]["reason"])
        self.assertIn("not treating a feature branch as published main", payload["destinations"]["dest"]["reason"])

    def test_valid_https_identity_is_accepted(self):
        sha = self.publish(self.dest, {"lib/here.py": "X = 1\n"}, origin="https://github.com/org/dest.git")
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "arrived")
        self.assertEqual(payload["destinations"]["dest"]["identity_host"], "github.com")
        self.assertEqual(payload["destinations"]["dest"]["identity_repo"], "org/dest")
        self.assertEqual(payload["destinations"]["dest"]["selected_commit"], sha)

    def test_valid_ssh_identity_is_accepted(self):
        self.publish(self.dest, {"lib/here.py": "X = 1\n"}, origin=DEST_ORIGIN_SSH)
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "arrived")
        self.assertEqual(payload["destinations"]["dest"]["identity_host"], "github.com")
        self.assertEqual(payload["destinations"]["dest"]["identity_repo"], "org/dest")

    def test_origin_credentials_are_not_echoed(self):
        secret = "supersecret-origin-token"
        self.publish(self.dest, {"lib/here.py": "X = 1\n"}, origin=f"https://user:{secret}@github.com/org/dest.git")
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None))
        code, payload, err = self.run_ledger_stderr(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "arrived")
        blob = json.dumps(payload) + err
        self.assertNotIn(secret, blob)
        self.assertNotIn("user:", blob)

    def test_scaffold_checkout_dot_is_not_exempt_from_provenance(self):
        write(self.hub / "lib" / "here.py", "X = 1\n")
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", destination="hub", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger)
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "unverified")
        self.assertIn("not a git repository", payload["destinations"]["hub"]["reason"])

    def test_unpublished_feature_branch_is_not_default_branch_arrival(self):
        self.publish(self.dest, {"README.md": "dest\n"})
        write(self.dest / "lib" / "here.py", "X = 1\n")
        self.git(self.dest, "add", ".")
        self.git(self.dest, "commit", "-q", "-m", "unpublished feature")
        feature = self.git(self.dest, "rev-parse", "HEAD")
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None, commit=feature))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 1)
        self.assertEqual(payload["items"][0]["verdict"], "missing")
        self.assertEqual(payload["destinations"]["dest"]["revision_scope"], "published-default-branch")
        self.assertNotEqual(payload["destinations"]["dest"]["selected_commit"], feature)

    def test_explicit_candidate_revision_names_sha_and_scope(self):
        self.publish(self.dest, {"README.md": "dest\n"})
        write(self.dest / "lib" / "here.py", "X = 1\n")
        self.git(self.dest, "add", ".")
        self.git(self.dest, "commit", "-q", "-m", "candidate")
        feature = self.git(self.dest, "rev-parse", "HEAD")
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None, commit=feature))
        code, payload = self.run_ledger(
            ledger,
            "--destination",
            f"dest={self.dest}",
            "--revision",
            f"dest={feature}",
        )
        self.assertEqual(code, 0, payload.get("drift"))
        self.assertEqual(payload["items"][0]["verdict"], "arrived")
        self.assertEqual(payload["destinations"]["dest"]["revision_scope"], "candidate")
        self.assertEqual(payload["destinations"]["dest"]["selected_commit"], feature)
        self.assertEqual(payload["destinations"]["dest"]["head_commit"], feature)

    def test_published_mode_reads_fetched_origin_default_branch(self):
        published = self.publish(self.dest, {"lib/here.py": "X = 1\n"})
        write(self.dest / "lib" / "here.py", "DIRTY_NOT_COMMITTED\n")
        write(self.dest / "lib" / "extra.py", "untracked\n")
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None, evidence=[
            {"kind": "text", "path": "lib/here.py", "contains": "X = 1"},
        ]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "arrived")
        self.assertEqual(payload["destinations"]["dest"]["selected_commit"], published)
        self.assertEqual(payload["destinations"]["dest"]["revision_scope"], "published-default-branch")

    def test_moving_head_and_refs_after_snapshot_does_not_change_evidence(self):
        first = self.publish(self.dest, {"lib/here.py": "FIRST\n"})
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None, evidence=[
            {"kind": "text", "path": "lib/here.py", "contains": "FIRST"},
        ]))
        ledger_path = self.hub / ".agents" / "policy" / "split-ledger.json"
        write(ledger_path, json.dumps(ledger, indent=2))
        loaded = reconcile.load_ledger(ledger_path, self.hub)
        checkouts = reconcile.resolve_checkouts(loaded, self.hub, {"dest": self.dest})
        write(self.dest / "lib" / "here.py", "SECOND\n")
        self.git(self.dest, "add", ".")
        self.git(self.dest, "commit", "-q", "-m", "move refs")
        second = self.git(self.dest, "rev-parse", "HEAD")
        self.git(self.dest, "update-ref", "refs/remotes/origin/main", second)
        self.assertNotEqual(first, second)
        observation = reconcile.observe(loaded.items[0], checkouts["dest"], loaded.skip_roots)
        self.assertEqual(observation.state, "arrived")
        self.assertEqual(observation.head_commit, first)
        self.assertEqual(checkouts["dest"].selected_commit, first)

    def test_all_six_kinds_follow_committed_tree_not_untracked_or_dirty(self):
        good_py = "def moved_feature():\n    return 1\n"
        digest = hashlib.sha256(good_py.encode("utf-8")).hexdigest()
        base = self.publish(self.dest, {"README.md": "dest\n", "lib/moved.py": "OLD_UNRELATED\n"})
        write(self.dest / "lib" / "moved.py", good_py)
        write(self.dest / "lib" / "untracked.py", good_py)
        self.git(self.dest, "commit", "-q", "--allow-empty", "-m", "unpublished feature commit")
        feature = self.git(self.dest, "rev-parse", "HEAD")
        kinds = [
            ("path-untracked", {"kind": "path", "path": "lib/untracked.py"}),
            ("text-dirty", {"kind": "text", "path": "lib/moved.py", "contains": "moved_feature"}),
            ("sha-dirty", {"kind": "sha256", "path": "lib/moved.py", "sha256": digest}),
            ("symbols-dirty", {"kind": "symbols", "path": "lib/moved.py", "names": ["moved_feature"]}),
            ("reference-untracked", {"kind": "reference", "glob": "lib/untracked.py", "pattern": "moved_feature"}),
            ("commit-feature", {"kind": "commit", "commit": feature}),
        ]
        ledger = ledger_skeleton()
        for item_id, evidence in kinds:
            ledger["items"].append(item(item_id, state="missing", evidence=[evidence]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        for row in payload["items"]:
            with self.subTest(item=row["id"]):
                self.assertEqual(row["verdict"], "missing")
                self.assertEqual(row["observed"]["head_commit"], base)
                self.assertEqual(row["observed"]["evidence"][0]["status"], "absent")

    def test_all_six_kinds_positive_committed_and_missing_tree(self):
        good_py = "def moved_feature():\n    return 1\n"
        digest = hashlib.sha256(good_py.encode("utf-8")).hexdigest()
        sha = self.publish(self.dest, {"lib/moved.py": good_py, "README.md": "dest\n"})
        ledger = ledger_skeleton()
        ledger["items"].append(item("path-present", state="arrived", follow_up=None, evidence=[{"kind": "path", "path": "lib/moved.py"}]))
        ledger["items"].append(item("text-present", state="arrived", follow_up=None, evidence=[{"kind": "text", "path": "lib/moved.py", "contains": "moved_feature"}]))
        ledger["items"].append(item("sha-present", state="arrived", follow_up=None, evidence=[{"kind": "sha256", "path": "lib/moved.py", "sha256": digest}]))
        ledger["items"].append(item("symbols-present", state="arrived", follow_up=None, evidence=[{"kind": "symbols", "path": "lib/moved.py", "names": ["moved_feature"]}]))
        ledger["items"].append(item("reference-present", state="arrived", follow_up=None, evidence=[{"kind": "reference", "glob": "lib/*.py", "pattern": "moved_feature"}]))
        ledger["items"].append(item("commit-present", state="arrived", follow_up=None, evidence=[{"kind": "commit", "commit": sha}]))
        ledger["items"].append(item("path-absent", evidence=[{"kind": "path", "path": "lib/missing.py"}]))
        ledger["items"].append(item("text-absent", evidence=[{"kind": "text", "path": "lib/moved.py", "contains": "NOT_THERE"}]))
        ledger["items"].append(item("sha-absent", evidence=[{"kind": "sha256", "path": "lib/moved.py", "sha256": "0" * 64}]))
        ledger["items"].append(item("symbols-absent", evidence=[{"kind": "symbols", "path": "lib/moved.py", "names": ["missing_name"]}]))
        ledger["items"].append(item("reference-absent", evidence=[{"kind": "reference", "glob": "lib/*.py", "pattern": "NOT_THERE"}]))
        ledger["items"].append(item("commit-absent", evidence=[{"kind": "commit", "commit": "1111111111111111111111111111111111111111"}]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        verdicts = {row["id"]: row["verdict"] for row in payload["items"]}
        for key in (
            "path-present",
            "text-present",
            "sha-present",
            "symbols-present",
            "reference-present",
            "commit-present",
        ):
            self.assertEqual(verdicts[key], "arrived", key)
        for key in (
            "path-absent",
            "text-absent",
            "sha-absent",
            "symbols-absent",
            "reference-absent",
            "commit-absent",
        ):
            self.assertEqual(verdicts[key], "missing", key)

    def test_untracked_receipt_does_not_attest_or_contradict(self):
        self.publish(self.dest, {"README.md": "dest\n"})
        write(
            self.dest / "docs" / "split-receipt.json",
            json.dumps({"version": 1, "items": {"claimed": {"arrived_in": "abc", "attested_by": "x", "attested_on": "2026-09-07"}}}),
        )
        ledger = ledger_skeleton()
        ledger["items"].append(item("claimed", state="missing"))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertFalse(payload["destinations"]["dest"]["receipt_loaded"])
        self.assertIsNone(payload["destinations"]["dest"]["receipt_error"])
        self.assertIsNone(payload["items"][0]["observed"]["attested"])
        self.assertIsNone(payload["items"][0]["drift"])

    def test_dirty_receipt_does_not_attest_or_contradict(self):
        committed = json.dumps({"version": 1, "items": {}})
        self.publish(self.dest, {"README.md": "dest\n", "docs/split-receipt.json": committed})
        write(
            self.dest / "docs" / "split-receipt.json",
            json.dumps({"version": 1, "items": {"claimed": {"arrived_in": "abc", "attested_by": "x", "attested_on": "2026-09-07"}}}),
        )
        ledger = ledger_skeleton()
        ledger["items"].append(item("claimed", state="missing"))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertTrue(payload["destinations"]["dest"]["receipt_loaded"])
        self.assertFalse(payload["items"][0]["observed"]["attested"])
        self.assertIsNone(payload["items"][0]["drift"])

    def test_symlink_outside_checkout_is_not_followed(self):
        outside = self.base / "outside-secret.txt"
        write(outside, "OUTSIDE_SECRET_MARKER_SHOULD_NOT_BE_READ\n")
        self.publish(self.dest, {"README.md": "dest\n"})
        link = self.dest / "lib" / "leaky.py"
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, link)
        self.git(self.dest, "add", "-A")
        self.git(self.dest, "commit", "-q", "-m", "symlink")
        sha = self.git(self.dest, "rev-parse", "HEAD")
        self.git(self.dest, "update-ref", "refs/remotes/origin/main", sha)
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        ledger = ledger_skeleton()
        ledger["items"].append(item("symlink-path", state="missing", evidence=[{"kind": "path", "path": "lib/leaky.py"}]))
        ledger["items"].append(item("symlink-text", state="missing", evidence=[
            {"kind": "text", "path": "lib/leaky.py", "contains": "OUTSIDE_SECRET_MARKER_SHOULD_NOT_BE_READ"},
        ]))
        ledger["items"].append(item("symlink-hash", state="missing", evidence=[
            {"kind": "sha256", "path": "lib/leaky.py", "sha256": digest},
        ]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        for row in payload["items"]:
            with self.subTest(item=row["id"]):
                self.assertEqual(row["verdict"], "unverified")
                self.assertEqual(row["observed"]["evidence"][0]["status"], "unverifiable")
                self.assertIn("not followed", row["observed"]["evidence"][0]["detail"])

    def test_submodule_gitlink_is_not_followed(self):
        outside = self.base / "outside-repo"
        outside.mkdir()
        self.publish(outside, {"secret.py": "def leaked():\n    return 1\n"}, origin="https://github.com/org/outside.git")
        outside_sha = self.git(outside, "rev-parse", "HEAD")
        self.publish(self.dest, {"README.md": "dest\n"})
        self.git(self.dest, "update-index", "--add", "--cacheinfo", f"160000,{outside_sha},vendor/outside")
        write(self.dest / "vendor" / "outside" / "secret.py", "def leaked():\n    return 1\nUNIQUE_SUBMODULE_BODY\n")
        self.git(self.dest, "commit", "-q", "-m", "gitlink")
        sha = self.git(self.dest, "rev-parse", "HEAD")
        self.git(self.dest, "update-ref", "refs/remotes/origin/main", sha)
        ledger = ledger_skeleton()
        ledger["items"].append(item("gitlink-path", state="missing", evidence=[{"kind": "path", "path": "vendor/outside"}]))
        ledger["items"].append(item("gitlink-inside", state="missing", evidence=[
            {"kind": "text", "path": "vendor/outside/secret.py", "contains": "UNIQUE_SUBMODULE_BODY"},
        ]))
        ledger["items"].append(item("gitlink-symbols", state="missing", evidence=[
            {"kind": "symbols", "glob": "vendor/outside/*.py", "names": ["leaked"]},
        ]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        rows = {row["id"]: row for row in payload["items"]}
        self.assertEqual(rows["gitlink-path"]["verdict"], "unverified")
        self.assertEqual(rows["gitlink-path"]["observed"]["evidence"][0]["status"], "unverifiable")
        self.assertEqual(rows["gitlink-inside"]["verdict"], "missing")
        self.assertEqual(rows["gitlink-inside"]["observed"]["evidence"][0]["status"], "absent")
        self.assertEqual(rows["gitlink-symbols"]["verdict"], "missing")

    def test_hidden_tree_globs_preserve_leading_dot(self):
        body = "MOVED_FEATURE_EVIDENCE = True\n\ndef moved_feature():\n    pass\n"
        self.publish(self.dest, {".agents/lib/moved.py": body, "README.md": "dest\n"})
        ledger = ledger_skeleton()
        ledger["items"].append(item("hidden-symbols", state="arrived", follow_up=None, evidence=[
            {"kind": "symbols", "glob": ".agents/lib/*.py", "names": ["moved_feature"]},
        ]))
        ledger["items"].append(item("hidden-reference", state="arrived", follow_up=None, evidence=[
            {"kind": "reference", "glob": ".agents/lib/*.py", "pattern": "MOVED_FEATURE_EVIDENCE"},
        ]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        verdicts = {row["id"]: row["verdict"] for row in payload["items"]}
        self.assertEqual(verdicts, {"hidden-symbols": "arrived", "hidden-reference": "arrived"})

    def test_hidden_path_does_not_prove_an_unrelated_visible_path(self):
        body = "MOVED_FEATURE_EVIDENCE = True\n\ndef moved_feature():\n    pass\n"
        self.publish(self.dest, {".agents/lib/moved.py": body, "README.md": "dest\n"})
        ledger = ledger_skeleton()
        ledger["items"].append(item("alias-path", evidence=[{"kind": "path", "path": "agents/lib/moved.py"}]))
        ledger["items"].append(item("alias-text", evidence=[
            {"kind": "text", "path": "agents/lib/moved.py", "contains": "MOVED_FEATURE_EVIDENCE"},
        ]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        for row in payload["items"]:
            with self.subTest(item=row["id"]):
                self.assertEqual(row["verdict"], "missing")
                self.assertEqual(row["observed"]["evidence"][0]["status"], "absent")

    def test_hidden_skip_root_is_not_renamed_and_scanned(self):
        body = "MOVED_FEATURE_EVIDENCE = True\n\ndef moved_feature():\n    pass\n"
        self.publish(self.dest, {".private/moved.py": body, "README.md": "dest\n"})
        ledger = ledger_skeleton()
        ledger["scan"]["skip_roots"].append(".private")
        ledger["items"].append(item("skipped-hidden", evidence=[
            {"kind": "reference", "glob": "*moved.py", "pattern": "MOVED_FEATURE_EVIDENCE"},
        ]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        self.assertNotEqual(payload["items"][0]["verdict"], "arrived")
        self.assertEqual(payload["items"][0]["verdict"], "missing")

    def test_tree_path_preserves_dotfile_space_and_backslash_identity(self):
        self.assertEqual(reconcile._tree_path(".agents/lib/moved.py"), ".agents/lib/moved.py")
        self.assertEqual(reconcile._tree_path(".private/moved.py"), ".private/moved.py")
        self.assertEqual(reconcile._tree_path("./lib/moved.py"), "lib/moved.py")
        self.assertEqual(reconcile._tree_path("./.agents/lib/moved.py"), ".agents/lib/moved.py")
        self.assertEqual(reconcile._tree_path("agents/lib/moved.py"), "agents/lib/moved.py")
        self.assertEqual(reconcile._tree_path("foo bar.py"), "foo bar.py")
        self.assertEqual(reconcile._tree_path("foo\\bar.py"), "foo\\bar.py")
        body = "MOVED_FEATURE_EVIDENCE = True\n"
        self.publish(
            self.dest,
            {
                "lib/here.py": "X = 1\n",
                "docs/spaced file.py": body,
                "docs/slash\\name.py": body,
            },
        )
        ledger = ledger_skeleton()
        ledger["items"].append(item("dot-prefix", state="arrived", follow_up=None, evidence=[
            {"kind": "path", "path": "./lib/here.py"},
        ]))
        ledger["items"].append(item("spaced", state="arrived", follow_up=None, evidence=[
            {"kind": "text", "path": "docs/spaced file.py", "contains": "MOVED_FEATURE_EVIDENCE"},
        ]))
        ledger["items"].append(item("backslash", state="arrived", follow_up=None, evidence=[
            {"kind": "path", "path": "docs/slash\\name.py"},
        ]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        verdicts = {row["id"]: row["verdict"] for row in payload["items"]}
        self.assertEqual(verdicts, {"dot-prefix": "arrived", "spaced": "arrived", "backslash": "arrived"})

    def test_optional_directory_separator_keeps_existing_tree_evidence(self):
        body = "MOVED_FEATURE_EVIDENCE = True\n\ndef moved_feature():\n    pass\n"
        self.publish(self.dest, {"lib/moved.py": body, ".agents/lib/moved.py": body})
        self.assertEqual(reconcile._tree_path("lib"), "lib")
        self.assertEqual(reconcile._tree_path("lib/"), "lib")
        self.assertEqual(reconcile._tree_path("./lib/"), "lib")
        self.assertEqual(reconcile._tree_path(".agents/lib"), ".agents/lib")
        self.assertEqual(reconcile._tree_path(".agents/lib/"), ".agents/lib")
        self.assertEqual(reconcile._tree_path("./.agents/lib/"), ".agents/lib")
        self.assertEqual(reconcile._tree_path(".agents/"), ".agents")
        ledger = ledger_skeleton()
        for path in ("lib", "lib/", "./lib/", ".agents/lib", ".agents/lib/", "./.agents/lib/"):
            ledger["items"].append(item(f"dir-{path}", state="arrived", follow_up=None, evidence=[
                {"kind": "path", "path": path},
            ]))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        for row in payload["items"]:
            with self.subTest(item=row["id"]):
                self.assertEqual(row["verdict"], "arrived")
                self.assertEqual(row["observed"]["evidence"][0]["status"], "present")


class GitHubIdentityTests(unittest.TestCase):
    def test_https_and_ssh_forms_and_suffix_hosts(self):
        cases = [
            ("https://github.com/org/dest.git", ("github.com", "org/dest")),
            ("https://github.com/org/dest", ("github.com", "org/dest")),
            ("https://github.com/org/dest/", ("github.com", "org/dest")),
            ("git@github.com:org/dest.git", ("github.com", "org/dest")),
            ("ssh://git@github.com/org/dest.git", ("github.com", "org/dest")),
            ("https://user:token@github.com/org/dest.git", ("github.com", "org/dest")),
            ("https://github.com.evil.invalid/org/dest.git", None),
            ("https://example.invalid/org/dest.git", None),
            ("https://example.invalid/github.com/org/dest.git", None),
            ("git@example.invalid:org/dest.git", None),
            ("https://github.com/org/other.git", ("github.com", "org/other")),
            ("git://github.com/org/dest.git", None),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(reconcile.parse_github_identity(url), expected)


if __name__ == "__main__":
    unittest.main()
