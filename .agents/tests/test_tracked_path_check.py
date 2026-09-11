#!/usr/bin/env python3
"""Regression tests for the tracked in-tree path guard."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / ".agents" / "scripts" / "tracked_path_check.py"
POLICY = ROOT / ".agents" / "policy" / "tracked-paths.json"
BASELINE = ROOT / ".agents" / "policy" / "tracked-paths-baseline.json"
# Split so this file is not a tracked reference to the extracted substrate path.
_OLD_SUBSTRATE = "." + "remote-dev"


def load_checker():
    spec = importlib.util.spec_from_file_location("_vaws_tracked_path_check_test", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = load_checker()


def invoke(*argv: str) -> tuple[int, dict]:
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
        "include_suffixes": [".md", ".yaml", ".yml", ".json", ".toml"],
        "skip_roots": ["vllm", "vllm-ascend"],
        "fixture_paths": [],
        "skip_paths": [],
    },
    "token_prefixes": [
        ".agents/",
        _OLD_SUBSTRATE + "/",
        ".claude/",
        ".codex/",
        ".cursor/",
        ".grok/",
        ".trae/",
        ".github/",
        "docs/",
        "scripts/",
    ],
    "allow_prefixes": [
        {"prefix": ".vaws-local/", "why": "runtime"},
        {"prefix": ".vaws-runtime/", "why": "runtime"},
        {"prefix": "<", "why": "placeholder"},
    ],
    "rules": [
        {
            "id": "missing-path",
            "kind": "missing-in-tree-path",
            "severity": "error",
            "why": "dead in-tree path",
        }
    ],
}

EMPTY_BASELINE = {"version": 1, "generated_on": "2026-01-01", "note": "test", "accepted": []}


class SyntheticRepo:
    def __init__(self, root: Path) -> None:
        self.root = root
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
        write(root / "docs" / "alive.md", "# alive\n\nStatus: current\n")
        self.policy_path = root / ".agents" / "policy" / "tracked-paths.json"
        self.baseline_path = root / ".agents" / "policy" / "tracked-paths-baseline.json"
        self.set_policy(SYNTHETIC_POLICY)
        self.set_baseline(EMPTY_BASELINE)
        self._git_add()

    def set_policy(self, payload: dict) -> None:
        write(self.policy_path, json.dumps(payload, indent=2) + "\n")

    def set_baseline(self, payload: dict) -> None:
        write(self.baseline_path, json.dumps(payload, indent=2) + "\n")

    def accept(self, *entries: dict) -> None:
        rows = []
        for entry in entries:
            row = {"removed_by": "historical-evidence", "accepted_on": "2026-01-01", "why": "recorded"}
            row.update(entry)
            rows.append(row)
        self.set_baseline({**EMPTY_BASELINE, "accepted": rows})
        self._git_add()

    def _git_add(self) -> None:
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True)

    def run(self, *argv: str) -> tuple[int, dict]:
        self._git_add()
        return invoke("--repo-root", str(self.root), *argv)


class RealTreeTests(unittest.TestCase):
    def test_the_only_scan_exemption_is_this_test_file(self) -> None:
        policy = guard.load_policy(POLICY, ROOT)
        self.assertEqual(policy.fixture_paths, frozenset({".agents/tests/test_tracked_path_check.py"}))

    def test_shipped_policy_forbids_remote_dev_state(self) -> None:
        prefixes = [item["prefix"] if isinstance(item, dict) else item for item in json.loads(POLICY.read_text(encoding="utf-8"))["allow_prefixes"]]
        forbidden = _OLD_SUBSTRATE + "/state"
        self.assertNotIn(forbidden + "/", prefixes)
        self.assertFalse(any(item.startswith(forbidden) for item in prefixes))


class DetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = SyntheticRepo(Path(self.temp.name))

    def test_a_dead_path_fails_enforce(self) -> None:
        write(self.repo.root / "docs" / "guide.md", "See `.agents/skills/missing/SKILL.md`.\n")
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["status"]), (1, "failed"))
        self.assertEqual(payload["counts"]["new"], 1)
        self.assertEqual(payload["new_violations"][0]["token"], ".agents/skills/missing/SKILL.md")
        self.assertEqual(payload["new_violations"][0]["resolved"], ".agents/skills/missing/SKILL.md")

    def test_the_same_path_in_the_baseline_passes(self) -> None:
        write(self.repo.root / "docs" / "guide.md", "See `.agents/skills/missing/SKILL.md`.\n")
        self.repo.accept(
            {
                "rule": "missing-path",
                "path": "docs/guide.md",
                "named_path": ".agents/skills/missing/SKILL.md",
            }
        )
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual(
            (code, payload["status"]),
            (0, "passed"),
            payload.get("new_violations") or payload.get("stale_baseline") or payload,
        )
        self.assertEqual(payload["counts"]["accepted"], 1)
        self.assertEqual(payload["counts"]["new"], 0)

    def test_a_baseline_row_with_no_match_fails_as_stale(self) -> None:
        write(self.repo.root / "docs" / "guide.md", "See `.agents/skills/missing/SKILL.md`.\n")
        self.repo.accept(
            {
                "rule": "missing-path",
                "path": "docs/guide.md",
                "named_path": ".agents/skills/missing/SKILL.md",
            },
            {
                "rule": "missing-path",
                "path": "docs/gone.md",
                "named_path": ".agents/skills/other/SKILL.md",
            },
        )
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["status"]), (1, "failed"))
        self.assertEqual([row["path"] for row in payload["stale_baseline"]], ["docs/gone.md"])

    def test_an_unassigned_row_fails(self) -> None:
        write(self.repo.root / "docs" / "guide.md", "See `.agents/skills/missing/SKILL.md`.\n")
        self.repo.accept(
            {
                "rule": "missing-path",
                "path": "docs/guide.md",
                "named_path": ".agents/skills/missing/SKILL.md",
                "removed_by": guard.UNATTRIBUTED,
            }
        )
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["status"]), (1, "failed"))
        self.assertEqual(payload["baseline"]["unattributed_count"], 1)

    def test_relative_link_resolution(self) -> None:
        write(self.repo.root / "docs" / "nested" / "guide.md", "See [missing](../absent.md).\n")
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["status"]), (1, "failed"))
        hit = payload["new_violations"][0]
        self.assertEqual(hit["token"], "../absent.md")
        self.assertEqual(hit["resolved"], "docs/absent.md")

        write(self.repo.root / "docs" / "absent.md", "# absent\n")
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["status"]), (0, "passed"))

    def test_anchor_and_line_suffix_stripping(self) -> None:
        write(
            self.repo.root / "docs" / "guide.md",
            "See `docs/alive.md#section` and `docs/alive.md:12-20`.\n",
        )
        code, payload = self.repo.run("--mode", "enforce")
        self.assertEqual((code, payload["status"]), (0, "passed"))
        self.assertEqual(payload["counts"]["violations"], 0)

    def test_report_mode_never_fails(self) -> None:
        write(self.repo.root / "docs" / "guide.md", "See `.agents/skills/missing/SKILL.md`.\n")
        code, payload = self.repo.run("--mode", "report")
        self.assertEqual((code, payload["status"]), (0, "reported"))
        self.assertEqual(payload["counts"]["new"], 1)

    def test_untracked_leftover_does_not_satisfy_existence(self) -> None:
        write(self.repo.root / "docs" / "guide.md", "See `.agents/skills/missing/SKILL.md`.\n")
        self.repo._git_add()
        leftover = self.repo.root / ".agents" / "skills" / "missing" / "SKILL.md"
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_text("# leftover\n", encoding="utf-8")
        self.assertTrue(leftover.exists())
        self.assertFalse(guard.path_exists(self.repo.root, ".agents/skills/missing/SKILL.md"))
        # Do not git-add the leftover; SyntheticRepo.run() would.
        code, payload = invoke("--repo-root", str(self.repo.root), "--mode", "enforce")
        self.assertEqual((code, payload["status"]), (1, "failed"))
        self.assertEqual(payload["counts"]["new"], 1)
        self.assertEqual(
            payload["new_violations"][0]["resolved"],
            ".agents/skills/missing/SKILL.md",
        )

    def test_untracked_leftover_does_not_stale_a_baseline_row(self) -> None:
        write(self.repo.root / "docs" / "guide.md", "See `.agents/skills/missing/SKILL.md`.\n")
        self.repo.accept(
            {
                "rule": "missing-path",
                "path": "docs/guide.md",
                "named_path": ".agents/skills/missing/SKILL.md",
            }
        )
        leftover = self.repo.root / ".agents" / "skills" / "missing" / "SKILL.md"
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_text("# leftover\n", encoding="utf-8")
        self.assertTrue(leftover.exists())
        code, payload = invoke("--repo-root", str(self.repo.root), "--mode", "enforce")
        self.assertEqual(
            (code, payload["status"]),
            (0, "passed"),
            payload.get("new_violations") or payload.get("stale_baseline") or payload,
        )
        self.assertEqual(payload["counts"]["accepted"], 1)
        self.assertEqual(payload["baseline"]["stale_count"], 0)


if __name__ == "__main__":
    unittest.main()
