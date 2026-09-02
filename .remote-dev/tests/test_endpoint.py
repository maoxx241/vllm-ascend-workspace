from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REPO_ROOT = ROOT.parent
AGENTS_LIB = str(REPO_ROOT / ".agents" / "lib")
if AGENTS_LIB not in sys.path:
    sys.path.insert(0, AGENTS_LIB)

from core.endpoint import Endpoint, resolve_endpoint  # noqa: E402
import vaws_session_id  # noqa: E402


class EndpointTests(unittest.TestCase):
    def test_endpoint_id_is_stable_and_redacts_from_state_path(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        self.assertEqual(endpoint.endpoint_id, Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace").endpoint_id)
        self.assertEqual(len(endpoint.endpoint_id), 16)
        self.assertNotIn("1.2.3.4", endpoint.endpoint_id)

    def test_direct_endpoint_defaults(self) -> None:
        endpoint = resolve_endpoint({"host": "1.2.3.4", "port": 46000})
        self.assertEqual(endpoint.user, "root")
        self.assertEqual(endpoint.root, "/")
        self.assertEqual(endpoint.effective_cwd, "/vllm-workspace")


class FindSessionBindingTests(unittest.TestCase):
    """The cwd-upward auto-bind walk stops at the repository root (D2)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.repo = base / "repo"
        self.deep = self.repo / "a" / "b"
        self.deep.mkdir(parents=True)
        self.elsewhere = base / "elsewhere"
        self.elsewhere.mkdir()
        self._write_binding(base, "sess-stray-above-repo")

    def _write_binding(self, directory: Path, session_id: str) -> None:
        state = directory / ".vaws-local"
        state.mkdir(parents=True, exist_ok=True)
        (state / "current-session.json").write_text(
            json.dumps({"schema_version": 1, "session_id": session_id, "source": "test"}),
            encoding="utf-8",
        )

    def test_walk_finds_binding_at_or_below_repo_root(self) -> None:
        self._write_binding(self.repo, "sess-in-repo")
        with mock.patch.object(vaws_session_id, "_binding_walk_stop", return_value=self.repo):
            found = vaws_session_id.find_session_binding(self.deep)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], self.repo)
        self.assertEqual(found[1]["session_id"], "sess-in-repo")

    def test_walk_ignores_stray_binding_above_repo_root(self) -> None:
        with mock.patch.object(vaws_session_id, "_binding_walk_stop", return_value=self.repo):
            found = vaws_session_id.find_session_binding(self.deep)
        self.assertIsNone(found)

    def test_walk_outside_repo_keeps_legacy_unbounded_behavior(self) -> None:
        # Documented fallback: when start is not beneath the repository the
        # stop is never reached and the walk continues to the filesystem root.
        with mock.patch.object(vaws_session_id, "_binding_walk_stop", return_value=self.repo):
            found = vaws_session_id.find_session_binding(self.elsewhere)
        self.assertIsNotNone(found)
        self.assertEqual(found[1]["session_id"], "sess-stray-above-repo")


if __name__ == "__main__":
    unittest.main()
