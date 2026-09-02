from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.job_ops as job_ops  # noqa: E402
import core.state_store as state_store  # noqa: E402
from core.endpoint import Endpoint  # noqa: E402
from core.ssh_transport import RemoteCompleted  # noqa: E402


class RemoteJobStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        original_root = state_store.substrate_root
        state_store.substrate_root = lambda: Path(self.temp.name)  # type: ignore[assignment]
        self.addCleanup(setattr, state_store, "substrate_root", original_root)
        self.endpoint = Endpoint(host="127.0.0.1", port=46000, root="/vllm-workspace")
        self.job_id = "job-test123"
        record = {
            "schema_version": "remote-dev.job.v1",
            "job_id": self.job_id,
            "target": self.endpoint.to_result_target(),
            "remote_dir": f"{self.endpoint.root}/.remote-dev/jobs/{self.job_id}",
            "started_at": "2026-09-01T00:00:00Z",
        }
        state_store.atomic_write_json(state_store.job_record_path(self.endpoint, self.job_id), record)

    def _status_with_stdout(self, stdout: str) -> dict:
        original = job_ops.run_script
        job_ops.run_script = lambda *_args, **_kwargs: RemoteCompleted(0, stdout, "")  # type: ignore[assignment]
        try:
            payload = job_ops.remote_job_status(self.endpoint, job_id=self.job_id)
        finally:
            job_ops.run_script = original  # type: ignore[assignment]
        return payload["result"]

    def test_corrupt_status_with_alive_pid_reports_running(self) -> None:
        # A half-written (corrupt) status.json while the pid is alive means
        # "not finalized yet", not failure — same as the missing-file branch.
        result = self._status_with_stdout("{not json\n__PID_ALIVE__=1\n")
        self.assertEqual(result["status"], "running")
        reason = result["job"]["remote_status"]["reason"]
        self.assertIn("not finalized", reason)

    def test_corrupt_status_with_dead_pid_reports_failed(self) -> None:
        result = self._status_with_stdout("{not json\n__PID_ALIVE__=0\n")
        self.assertEqual(result["status"], "failed")
        self.assertIn("corrupt", result["job"]["remote_status"]["reason"])

    def test_empty_status_with_alive_pid_reports_running(self) -> None:
        # An empty status.json cats to nothing, so stdout carries only the
        # pid sentinel; it must still resolve the pid and report running.
        result = self._status_with_stdout("__PID_ALIVE__=1\n")
        self.assertEqual(result["status"], "running")
        self.assertTrue(result["job"]["pid_alive"])

    def test_missing_status_with_alive_pid_reports_running(self) -> None:
        result = self._status_with_stdout("__STATUS_MISSING__\n__PID_ALIVE__=1\n")
        self.assertEqual(result["status"], "running")
        self.assertIn("not written yet", result["job"]["remote_status"]["reason"])

    def test_finalized_status_is_reported_as_written(self) -> None:
        result = self._status_with_stdout(
            '{"status":"succeeded","job_id":"job-test123","exit_code":0}\n__PID_ALIVE__=0\n'
        )
        self.assertEqual(result["status"], "succeeded")


class RemoteJobTailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        original_root = state_store.substrate_root
        state_store.substrate_root = lambda: Path(self.temp.name)  # type: ignore[assignment]
        self.addCleanup(setattr, state_store, "substrate_root", original_root)
        self.endpoint = Endpoint(host="127.0.0.1", port=46000, root="/vllm-workspace")
        self.job_id = "job-tail123"
        record = {
            "schema_version": "remote-dev.job.v1",
            "job_id": self.job_id,
            "target": self.endpoint.to_result_target(),
            "remote_dir": f"{self.endpoint.root}/.remote-dev/jobs/{self.job_id}",
            "started_at": "2026-09-01T00:00:00Z",
        }
        state_store.atomic_write_json(state_store.job_record_path(self.endpoint, self.job_id), record)

    def _tail_with_stdout(self, stdout: str) -> dict:
        original = job_ops.run_script
        job_ops.run_script = lambda *_args, **_kwargs: RemoteCompleted(0, stdout, "")  # type: ignore[assignment]
        try:
            payload = job_ops.remote_job_tail(self.endpoint, job_id=self.job_id)
        finally:
            job_ops.run_script = original  # type: ignore[assignment]
        return payload["result"]

    def test_sentinel_text_inside_log_content_is_not_a_missing_log(self) -> None:
        # The sentinel appears as log *content* here; only a sentinel in the
        # first line of a section marks that log as missing (D5).
        stdout = (
            "__STDOUT__\n"
            "worker boot ok\n"
            "previous run ended with __STDOUT___MISSING before the fix\n"
            "__STDERR__\n"
            "no errors\n"
        )
        result = self._tail_with_stdout(stdout)
        self.assertEqual(result["missing_logs"], [])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["warnings"], [])

    def test_sentinel_as_first_section_line_marks_only_that_log_missing(self) -> None:
        stdout = "__STDOUT__\n__STDOUT___MISSING\n__STDERR__\ntraceback line\n"
        result = self._tail_with_stdout(stdout)
        self.assertEqual(result["missing_logs"], ["stdout"])
        self.assertEqual(result["status"], "ok")
        self.assertIn("stdout.log does not exist", result["warnings"][0])

    def test_all_requested_logs_missing_is_log_not_found(self) -> None:
        stdout = "__STDOUT__\n__STDOUT___MISSING\n__STDERR__\n__STDERR___MISSING\n"
        result = self._tail_with_stdout(stdout)
        self.assertEqual(result["missing_logs"], ["stdout", "stderr"])
        self.assertEqual(result["status"], "log_not_found")
        self.assertEqual(result["outcome"], "failed")


if __name__ == "__main__":
    unittest.main()
