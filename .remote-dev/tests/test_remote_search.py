from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.endpoint import Endpoint  # noqa: E402
from core.preview import MAX_GREP_MATCHES, MAX_TEXT_CHARS  # noqa: E402
import core.search_ops as search_ops  # noqa: E402


class RemoteSearchTests(unittest.TestCase):
    def test_remote_glob_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = search_ops.remote_glob(endpoint, pattern="*", path="/etc")
        self.assertEqual(payload["result"]["outcome"], "blocked")

    def test_remote_grep_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = search_ops.remote_grep(endpoint, pattern="x", path="/etc")
        self.assertEqual(payload["result"]["outcome"], "blocked")

    def test_remote_search_script_does_not_spawn_login_shell_to_find_rg(self) -> None:
        self.assertNotIn("bash\", \"-lc", search_ops.REMOTE_SEARCH_PY)
        self.assertIn("shutil.which(\"rg\")", search_ops.REMOTE_SEARCH_PY)

    def test_remote_grep_clamps_limit_and_text(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_runner = search_ops.run_remote_python
        captured = {}
        try:
            def fake_run_remote_python(_endpoint, _script, payload, **_kwargs):
                captured.update(payload)
                return {
                    "status": "ok",
                    "engine": "rg",
                    "output_mode": "content",
                    "matches": ["x" * (MAX_TEXT_CHARS * 2)],
                    "truncated": False,
                    "warnings": [],
                }

            search_ops.run_remote_python = fake_run_remote_python  # type: ignore[assignment]
            payload = search_ops.remote_grep(endpoint, pattern="x", path="/vllm-workspace", output_mode="content", limit=100000)
            self.assertEqual(captured["limit"], MAX_GREP_MATCHES)
            self.assertIn("clamped", payload["result"]["warnings"][0])
            self.assertLessEqual(len(payload["text"]), MAX_TEXT_CHARS)
        finally:
            search_ops.run_remote_python = original_runner  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
