#!/usr/bin/env python3
"""Regression tests for the split reconciliation checker and its ledger.

Two halves. The first runs the shipped ledger against this tree, so the rows
whose destination is the scaffold cannot drift from what the scaffold actually
contains, and the three known gaps stay recorded as gaps until somebody flips
them. The second builds throwaway destination checkouts in a temp dir and
proves the mechanics the design promises: a declared-but-absent item is
`missing`; an unreachable destination is `unverified` and never `arrived`; a
row whose item has since arrived fails until the row is updated; a row with no
attributed destination is rejected outright; and a destination receipt cannot
manufacture an arrival that the evidence contradicts.

Nothing here touches the network or a remote host.
"""

from __future__ import annotations

import contextlib
import copy
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


def load_checker():
    spec = importlib.util.spec_from_file_location("_vaws_split_reconcile_test", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reconcile = load_checker()


def invoke(*argv: str) -> tuple[int, dict]:
    """Run the checker's argv entry point and return (exit code, payload)."""
    out, err = io.StringIO(), io.StringIO()
    env_backup = os.environ.pop("VAWS_SPLIT_DESTINATIONS", None)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = reconcile.main(list(argv))
    finally:
        if env_backup is not None:
            os.environ["VAWS_SPLIT_DESTINATIONS"] = env_backup
    return code, json.loads(out.getvalue())


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
        self.assertEqual(code, 0, payload.get("drift"))
        self.assertEqual(payload["status"], "passed")
        for row in payload["items"]:
            with self.subTest(item=row["id"]):
                if row["destination"]["repo"] == "scaffold":
                    # The scaffold is this checkout: its rows are always
                    # observed, and the shipped ledger must agree with the tree.
                    self.assertEqual(row["verdict"], row["recorded"]["state"])
                else:
                    self.assertEqual(row["verdict"], "unverified")
        for repo_id, destination in payload["destinations"].items():
            if repo_id != "scaffold":
                self.assertFalse(destination["reachable"])

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


class SyntheticLedgerTests(unittest.TestCase):
    """Throwaway hub + destination checkouts in a temp dir."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.hub = self.base / "hub"
        self.dest = self.base / "dest"
        self.hub.mkdir()
        self.dest.mkdir()
        write(self.hub / "README.md", "hub\n")

    def run_ledger(self, ledger: dict, *argv: str) -> tuple[int, dict]:
        ledger_path = self.hub / ".agents" / "policy" / "split-ledger.json"
        write(ledger_path, json.dumps(ledger, indent=2))
        return invoke("--repo-root", str(self.hub), "--ledger", str(ledger_path), *argv)

    def blocked(self, ledger: dict, *argv: str) -> str:
        code, payload = self.run_ledger(ledger, *argv)
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["status"], "blocked")
        return payload["error"]

    # -- the three verdicts -------------------------------------------------

    def test_declared_but_absent_item_is_missing(self):
        ledger = ledger_skeleton()
        ledger["items"].append(item("gone"))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        row = payload["items"][0]
        self.assertEqual(row["verdict"], "missing")
        self.assertEqual(row["observed"]["evidence"][0]["status"], "absent")
        self.assertEqual(payload["counts"]["by_verdict"]["missing"], 1)

    def test_present_item_is_arrived_and_records_the_inspected_head(self):
        write(self.dest / "lib" / "here.py", "X = 1\n")
        ledger = ledger_skeleton()
        ledger["items"].append(item("here", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "arrived")
        # A non-git checkout is still inspectable; it just has no commit.
        self.assertIsNone(payload["items"][0]["observed"]["head_commit"])

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
        write(self.dest / "lib" / "thing.py", "X = 1\n")
        ledger = ledger_skeleton()
        ledger["items"].append(
            item(
                "thing",
                state="arrived",
                follow_up=None,
                evidence=[
                    {"kind": "path", "path": "lib/thing.py"},
                    # A commit check needs git metadata; the destination is a
                    # plain directory, so this piece cannot be established.
                    {"kind": "commit", "commit": "0123456789abcdef0123456789abcdef01234567"},
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
        ledger = ledger_skeleton()
        ledger["items"].append(item("landed", state="missing"))
        # Before the fix lands: agreement, pass.
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"][0]["verdict"], "missing")

        # The destination lands the item; the ledger still says missing.
        write(self.dest / "lib" / "landed.py", "X = 1\n")
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
        ledger = ledger_skeleton()
        ledger["items"].append(item("regressed", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 1)
        self.assertEqual(payload["drift"][0]["kind"], "regression")

    def test_recorded_unverified_fails_once_the_destination_is_inspected(self):
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
        write(self.dest / "tests" / "test_a.py", "class T:\n    def test_one(self):\n        pass\n")
        write(self.dest / "tests" / "test_b.py", "def test_two():\n    pass\nTABLE = {}\n")
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
        write(self.dest / "tests" / "test_b.py", "def test_two():\n    pass\nTABLE = {}\n# def test_three(): pass\nS = 'def test_three'\n")
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0, payload["drift"])
        verdicts = {row["id"]: row["verdict"] for row in payload["items"]}
        self.assertEqual(verdicts, {"suite": "arrived", "split-suite": "missing", "commented-out": "missing"})

    def test_sha256_text_and_reference_evidence(self):
        write(self.dest / "lib" / "vendor.py", "PIN = 1\n")
        digest = reconcile.hashlib.sha256(b"PIN = 1\n").hexdigest()
        write(self.dest / "README.md", "## Serving\nthe tools are served here\n")
        write(self.dest / "server.py", "from vaws_ops import TOOL_SCHEMAS\n")
        write(self.dest / "lib" / "vaws_ops.py", "TOOL_SCHEMAS = {}\n")
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
        write(self.dest / "node_modules" / "pkg" / "hit.py", "def test_hidden(): pass\n")
        write(self.dest / "vllm" / "hit.py", "def test_hidden(): pass\n")
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
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(self.dest), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()

        git("init", "-q")
        write(self.dest / "lib" / "x.py", "X = 1\n")
        git("add", ".")
        git("commit", "-q", "-m", "first")
        first = git("rev-parse", "HEAD")
        git("remote", "add", "origin", "git@example.invalid:org/dest.git")

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
        git("remote", "set-url", "origin", "git@example.invalid:org/other.git")
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertTrue(all(row["verdict"] == "unverified" for row in payload["items"]))
        self.assertIn("does not belong", payload["destinations"]["dest"]["reason"])

    # -- receipts ----------------------------------------------------------

    def test_receipt_cannot_manufacture_an_arrival_and_agreement_is_marked_attested(self):
        write(self.dest / "lib" / "real.py", "X = 1\n")
        write(self.dest / "docs" / "split-receipt.json", json.dumps({
            "version": 1,
            "items": {
                "real": {"arrived_in": "abc1234", "attested_by": "dest maintainer", "attested_on": "2026-09-07"},
                "claimed": {"arrived_in": "abc1234", "attested_by": "dest maintainer", "attested_on": "2026-09-07"},
            },
        }))
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
        write(self.dest / "lib" / "real.py", "X = 1\n")
        write(self.dest / "docs" / "split-receipt.json", "{not json")
        ledger = ledger_skeleton()
        ledger["items"].append(item("real", state="arrived", follow_up=None))
        code, payload = self.run_ledger(ledger, "--destination", f"dest={self.dest}")
        self.assertEqual(code, 0)
        self.assertIn("unreadable", payload["destinations"]["dest"]["receipt_error"])
        self.assertIsNone(payload["items"][0]["observed"]["attested"])


if __name__ == "__main__":
    unittest.main()
