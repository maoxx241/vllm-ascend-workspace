"""Local end-to-end checks for the REMOTE_SEARCH_PY grep -E fallback.

The fallback normally runs on the remote host over SSH; here the same script
runs locally with PATH masked so ``shutil.which("rg")`` fails and the
``grep -E`` path is exercised for real: ERE regex semantics, fail-closed
invalid regex, rg-aligned .git/hidden-dir exclusion, and fail-fast on
path-anchored globs (D1/D4).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.search_ops as search_ops  # noqa: E402


class GrepFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.grep = shutil.which("grep")
        if cls.grep is None:
            raise unittest.SkipTest("grep is not available on this host")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name) / "tree"
        (self.tree / "sub").mkdir(parents=True)
        (self.tree / ".git").mkdir()
        (self.tree / ".hidden").mkdir()
        (self.tree / "a.py").write_text("foo123\n", encoding="utf-8")
        (self.tree / "b.txt").write_text("food\n", encoding="utf-8")
        (self.tree / "sub" / "c.py").write_text("foo42\n", encoding="utf-8")
        (self.tree / ".git" / "config").write_text("foo99\n", encoding="utf-8")
        (self.tree / ".hidden" / "d.py").write_text("foo77\n", encoding="utf-8")
        # A PATH exposing grep but not rg forces the grep -E fallback.
        self.bindir = Path(self.temp.name) / "bin"
        self.bindir.mkdir()
        os.symlink(self.grep, self.bindir / "grep")

    def run_search(self, **overrides) -> dict:
        payload = {
            "op": "grep",
            "root": str(self.tree),
            "path": str(self.tree),
            "pattern": "foo[0-9]+",
            "output_mode": "files_with_matches",
        }
        payload.update(overrides)
        env = dict(os.environ)
        env["PATH"] = str(self.bindir)
        proc = subprocess.run(
            [sys.executable, "-c", search_ops.REMOTE_SEARCH_PY],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_fallback_preserves_ere_regex_semantics(self) -> None:
        data = self.run_search()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["engine"], "grep")
        matches = set(data["matches"])
        # ERE quantifier: "foo[0-9]+" matches digit-suffixed hits only.
        self.assertEqual(matches, {str(self.tree / "a.py"), str(self.tree / "sub" / "c.py")})
        self.assertTrue(any("grep -E fallback" in warning for warning in data["warnings"]))

    def test_fallback_excludes_git_and_hidden_dirs_like_rg(self) -> None:
        data = self.run_search()
        self.assertEqual(data["status"], "ok")
        self.assertFalse(any("/.git/" in match or "/.hidden/" in match for match in data["matches"]))

    def test_fallback_still_searches_an_explicit_hidden_base(self) -> None:
        data = self.run_search(path=str(self.tree / ".hidden"))
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["matches"], [str(self.tree / ".hidden" / "d.py")])

    def test_fallback_basename_glob_include_is_honored(self) -> None:
        data = self.run_search(glob="*.py")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(set(data["matches"]), {str(self.tree / "a.py"), str(self.tree / "sub" / "c.py")})

    def test_fallback_path_anchored_glob_fails_fast(self) -> None:
        data = self.run_search(glob="sub/*.py")
        self.assertEqual(data["status"], "rg_required")
        self.assertIn("path-anchored", data["error"])

    def test_fallback_invalid_regex_fails_closed(self) -> None:
        # grep exits 2 on an invalid ERE; the fallback must surface that as a
        # failure, never as a semantically wrong "ok" with zero matches.
        data = self.run_search(pattern="([")
        self.assertEqual(data["status"], "failed")
        self.assertTrue(data.get("error"))

    def test_fallback_count_mode_drops_zero_count_files(self) -> None:
        data = self.run_search(pattern="foo", output_mode="count")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(len(data["matches"]), 3)
        self.assertFalse(any(match.endswith(":0") for match in data["matches"]))


if __name__ == "__main__":
    unittest.main()
