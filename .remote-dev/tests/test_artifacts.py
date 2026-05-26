from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.endpoint import Endpoint  # noqa: E402
import core.artifact_ops as artifact_ops  # noqa: E402
import core.state_store as state_store  # noqa: E402


class ArtifactTests(unittest.TestCase):
    def test_artifact_manifest_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = artifact_ops.remote_artifact_manifest(endpoint, remote_path="/etc/passwd")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["status"], "path_outside_root")

    def test_artifact_manifest_persists_local_manifest_ref(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = artifact_ops.run_remote_python
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                artifact_ops.run_remote_python = lambda *_args, **_kwargs: {  # type: ignore[assignment]
                    "schema_version": "remote-dev.artifact_manifest.v1",
                    "status": "ok",
                    "root": "/vllm-workspace/out",
                    "is_dir": False,
                    "file_count": 1,
                    "total_bytes": 7,
                    "files": [],
                }
                payload = artifact_ops.remote_artifact_manifest(endpoint, remote_path="/vllm-workspace/out")
                manifest_ref = payload["result"]["refs"]["local_manifest"]
                self.assertTrue(Path(manifest_ref).exists())
                self.assertEqual(payload["result"]["artifacts"][0]["endpoint_id"], endpoint.endpoint_id)
                self.assertIn("artifact_id", payload["result"]["artifacts"][0])
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            artifact_ops.run_remote_python = original_runner  # type: ignore[assignment]

    def test_artifact_push_rejects_local_symlink(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.txt"
            target.write_text("secret\n", encoding="utf-8")
            link = Path(tmp) / "link.txt"
            link.symlink_to(target)
            payload = artifact_ops.remote_artifact_push(
                endpoint,
                local_path=str(link),
                remote_path="/vllm-workspace/out/link.txt",
            )
            self.assertEqual(payload["result"]["outcome"], "blocked")
            self.assertEqual(payload["result"]["status"], "symlink_not_allowed")

    def test_artifact_push_streams_and_verifies_hash(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        observed_calls = []
        original = artifact_ops.run_bytes
        try:
            with tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "artifact.txt"
                local.write_text("payload\n", encoding="utf-8")
                expected = artifact_ops._sha256_file(local)

                def fake_run_bytes(_endpoint, command, *, stdin=None, timeout_ms=None):
                    observed_calls.append({"command": command, "stdin": stdin, "timeout_ms": timeout_ms})
                    return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=(expected + "\n").encode(), stderr=b"")

                artifact_ops.run_bytes = fake_run_bytes  # type: ignore[assignment]
                payload = artifact_ops.remote_artifact_push(
                    endpoint,
                    local_path=str(local),
                    remote_path="/vllm-workspace/out/artifact.txt",
                )
                self.assertEqual(payload["result"]["outcome"], "success")
                self.assertEqual(payload["result"]["artifacts"][0]["pushed"][0]["sha256"], expected)
                self.assertEqual(observed_calls[0]["stdin"], b"payload\n")
                self.assertIn("mv -f", observed_calls[0]["command"])
        finally:
            artifact_ops.run_bytes = original  # type: ignore[assignment]

    def test_artifact_pull_blocks_malicious_relpath(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_manifest = artifact_ops.remote_artifact_manifest
        try:
            artifact_ops.remote_artifact_manifest = lambda *_args, **_kwargs: {  # type: ignore[assignment]
                "text": "",
                "result": {
                    "manifest": {
                        "status": "ok",
                        "files": [{
                            "relpath": "../escape.txt",
                            "path": "/vllm-workspace/out/file.txt",
                            "sha256": "0" * 64,
                            "size": 1,
                        }],
                    }
                },
            }
            with tempfile.TemporaryDirectory() as tmp:
                payload = artifact_ops.remote_artifact_pull(
                    endpoint,
                    remote_path="/vllm-workspace/out",
                    local_dir=tmp,
                )
            self.assertEqual(payload["result"]["outcome"], "blocked")
            self.assertEqual(payload["result"]["status"], "path_traversal")
        finally:
            artifact_ops.remote_artifact_manifest = original_manifest  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
