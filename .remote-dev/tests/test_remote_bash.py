from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.endpoint import Endpoint  # noqa: E402
from core.preview import MAX_JOB_TAIL_LINES, MAX_TEXT_CHARS  # noqa: E402
from core.ssh_transport import RemoteCompleted  # noqa: E402
import core.shell_ops as shell_ops  # noqa: E402
import core.state_store as state_store  # noqa: E402
import core.job_ops as job_ops  # noqa: E402


class RemoteBashTests(unittest.TestCase):
    def test_remote_bash_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = shell_ops.remote_bash(endpoint, command="pwd", cwd="/tmp")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["status"], "cwd_outside_root")
        self.assertEqual(
            payload["result"]["next"]["endpoint_patch"],
            {"root": "/tmp", "cwd": "/tmp"},
        )
        self.assertIn("--root /tmp --cwd /tmp", payload["text"])

    def test_remote_bash_core_allows_secret_like_argv(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = shell_ops.run_script
        scripts = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]

                def fake_run_script(_endpoint, script, **_kwargs):
                    scripts.append(script)
                    return RemoteCompleted(0, "ok\n", "")

                shell_ops.run_script = fake_run_script  # type: ignore[assignment]
                payload = shell_ops.remote_bash(endpoint, command="echo token=abc")
                self.assertEqual(payload["result"]["outcome"], "success")
                self.assertIn("echo token=abc", scripts[0])
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            shell_ops.run_script = original_runner  # type: ignore[assignment]

    def test_remote_bash_relative_cwd_hint_does_not_suggest_bad_root(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = shell_ops.remote_bash(endpoint, command="pwd", cwd="tmp")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["next"]["suggested_action"], "rerun_with_absolute_cwd")
        self.assertNotIn("endpoint_patch", payload["result"]["next"])

    def test_remote_bash_success_writes_log_refs(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = shell_ops.run_script
        scripts = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]

                def fake_run_script(_endpoint, script, **_kwargs):
                    scripts.append(script)
                    return RemoteCompleted(0, "ok\n", "")

                shell_ops.run_script = fake_run_script  # type: ignore[assignment]
                payload = shell_ops.remote_bash(endpoint, command="echo ok")
                self.assertEqual(payload["result"]["outcome"], "success")
                self.assertTrue(Path(payload["result"]["refs"]["stdout"]).exists())
                self.assertIn('bash -c "$REMOTE_DEV_COMMAND"', scripts[0])
                self.assertNotIn("bash -lc", scripts[0])
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            shell_ops.run_script = original_runner  # type: ignore[assignment]

    def test_background_remote_bash_missing_cwd_does_not_start_job(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = job_ops.run_script
        calls = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]

                def fake_run_script(_endpoint, script, **_kwargs):
                    calls.append(script)
                    return RemoteCompleted(70, "", "REMOTE_DEV_CWD_NOT_FOUND\n")

                job_ops.run_script = fake_run_script  # type: ignore[assignment]
                payload = shell_ops.remote_bash(
                    endpoint,
                    command="touch /vllm-workspace/should-not-exist",
                    cwd="/vllm-workspace/missing",
                    run_in_background=True,
                )
                self.assertEqual(payload["result"]["outcome"], "failed")
                self.assertEqual(payload["result"]["status"], "cwd_not_found")
                self.assertEqual(len(calls), 1)
                self.assertNotIn("touch /vllm-workspace/should-not-exist", calls[0])
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            job_ops.run_script = original_runner  # type: ignore[assignment]

    def test_background_remote_bash_duplicate_job_id_is_blocked(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = job_ops.run_script
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                state_store.atomic_write_json(state_store.job_record_path(endpoint, "job-existing"), {"job_id": "job-existing", "target": endpoint.to_result_target()})
                job_ops.run_script = lambda *_args, **_kwargs: RemoteCompleted(0, "", "")  # type: ignore[assignment]
                payload = job_ops.start_remote_job(endpoint, command="echo ok", job_id="job-existing")
                self.assertEqual(payload["result"]["outcome"], "blocked")
                self.assertEqual(payload["result"]["status"], "job_id_exists")
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            job_ops.run_script = original_runner  # type: ignore[assignment]

    def test_remote_job_tail_clamps_lines_and_text(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = job_ops.run_script
        scripts = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                job_id = "job-tail-test"
                state_store.atomic_write_json(
                    state_store.job_record_path(endpoint, job_id),
                    {"job_id": job_id, "target": endpoint.to_result_target(), "remote_dir": "/vllm-workspace/.remote-dev/jobs/job-tail-test"},
                )

                def fake_run_script(_endpoint, script, **_kwargs):
                    scripts.append(script)
                    return RemoteCompleted(0, "x" * (MAX_TEXT_CHARS * 2), "")

                job_ops.run_script = fake_run_script  # type: ignore[assignment]
                payload = job_ops.remote_job_tail(None, job_id=job_id, lines=100000)
                self.assertIn(f"tail -n {MAX_JOB_TAIL_LINES}", scripts[0])
                self.assertIn("clamped", payload["result"]["warnings"][0])
                self.assertLessEqual(len(payload["text"]), MAX_TEXT_CHARS)
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            job_ops.run_script = original_runner  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
