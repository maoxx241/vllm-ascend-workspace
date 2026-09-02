#!/usr/bin/env python3
"""Session lookup degrades on a missing index but warns on a corrupted one."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_session_state import SessionStateError, load_session_lookup, sessions_root


class SessionLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS /var is a /private/var symlink; resolve once so the state root
        # matches the resolved paths reported by load_session_lookup.
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def lookup(self):
        # Hermetic: ignore any real worktree binding above the pytest cwd.
        with mock.patch("vaws_session_state.find_session_binding", return_value=None):
            return load_session_lookup(session_id="task", repo_root=self.root)

    def test_missing_index_degrades_without_a_warning(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaisesRegex(SessionStateError, "not found in any known session index"):
                self.lookup()
        self.assertEqual(stderr.getvalue(), "")

    def test_corrupted_index_warns_and_degrades(self):
        index = sessions_root(self.root) / "index.json"
        index.parent.mkdir(parents=True)
        index.write_text("{not json")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaisesRegex(SessionStateError, "not found in any known session index"):
                self.lookup()
        self.assertIn("corrupted", stderr.getvalue())
        self.assertIn(str(self.root), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
