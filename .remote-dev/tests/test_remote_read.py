from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.endpoint import Endpoint  # noqa: E402
from core.preview import MAX_TEXT_CHARS  # noqa: E402
import core.file_ops as file_ops  # noqa: E402
import core.state_store as state_store  # noqa: E402


class RemoteReadTests(unittest.TestCase):
    def test_remote_read_writes_ledger(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = file_ops.run_remote_python
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                file_ops.run_remote_python = lambda *_args, **_kwargs: {  # type: ignore[assignment]
                    "status": "ok",
                    "file": {
                        "path": "/vllm-workspace/foo.py",
                        "sha256": "abc",
                        "size": 3,
                        "mtime_ns": 1,
                        "offset": 1,
                        "limit": 200,
                        "line_start": 1,
                        "line_end": 1,
                        "total_lines": 1,
                        "partial": False,
                        "content": "1 | abc",
                    },
                }
                payload = file_ops.remote_read(endpoint, file_path="/vllm-workspace/foo.py")
                self.assertEqual(payload["result"]["outcome"], "success")
                self.assertTrue(Path(payload["result"]["refs"]["read_ledger"]).exists())
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            file_ops.run_remote_python = original_runner  # type: ignore[assignment]

    def test_remote_read_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = file_ops.remote_read(endpoint, file_path="/etc/passwd")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["status"], "path_outside_root")

    def test_remote_read_clamps_excessive_limit_and_text(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = file_ops.run_remote_python
        captured = {}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]

                def fake_run_remote_python(_endpoint, _script, payload, **_kwargs):
                    captured.update(payload)
                    return {
                        "status": "partial",
                        "file": {
                            "path": "/vllm-workspace/big.log",
                            "sha256": "abc",
                            "size": 20000,
                            "mtime_ns": 1,
                            "offset": 1,
                            "limit": payload["limit"],
                            "line_start": 1,
                            "line_end": payload["limit"],
                            "total_lines": 100000,
                            "partial": True,
                            "content": "1 | " + ("x" * 20000),
                        },
                    }

                file_ops.run_remote_python = fake_run_remote_python  # type: ignore[assignment]
                payload = file_ops.remote_read(endpoint, file_path="/vllm-workspace/big.log", limit=100000)
                self.assertEqual(captured["limit"], file_ops.MAX_READ_LINES)
                self.assertIn("clamped", payload["result"]["warnings"][0])
                self.assertLessEqual(len(payload["text"]), MAX_TEXT_CHARS)
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            file_ops.run_remote_python = original_runner  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
