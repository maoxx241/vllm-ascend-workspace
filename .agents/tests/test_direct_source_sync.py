"""Direct source publication uses the installed parity contract, not a skill path."""

from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))

from vaws_coordinator.parity import build_parser  # noqa: E402
import vaws_remote_adapters as adapters  # noqa: E402


class DirectSourceSyncTests(unittest.TestCase):
    def test_cli_missing_host_returns_a_failure_without_remote_io(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / ".agents/scripts/remote_sync_plan.py")],
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "needs_input")

    def test_adapter_arguments_are_accepted_by_the_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "中文 sources"
            args = SimpleNamespace(
                host="192.0.2.10", port=2222, user="developer", repo_root=root,
                runtime_root="/prepared/source", source=[f"vllm={root / 'vllm'}"],
            )
            with mock.patch.object(adapters, "run_json_command") as run:
                plan = adapters.sync_plan(args, mode="source-only")
                run.assert_not_called()
            parsed = build_parser().parse_args(plan["command"][3:])
            self.assertEqual(parsed.container_host, args.host)
            self.assertEqual(parsed.container_port, 2222)
            self.assertEqual(parsed.container_user, "developer")
            self.assertEqual(parsed.apply_mode, "source-only")
            self.assertEqual(parsed.source, args.source)
            self.assertEqual(parsed.runtime_root, "/prepared/source")
            self.assertFalse(parsed.force_reinstall)

    def test_apply_invokes_package_with_explicit_source_only_and_dry_run(self):
        args = SimpleNamespace(host="192.0.2.10", source=[])
        result = (0, {"status": "dry-run"}, '{"status":"dry-run"}', "")
        with mock.patch.object(adapters, "run_json_command", return_value=result) as run:
            payload = adapters.sync_apply(args, mode="auto", dry_run=True)
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["-m", "vaws_coordinator.parity"])
        parsed = build_parser().parse_args(command[3:])
        self.assertTrue(parsed.dry_run)
        self.assertEqual(parsed.apply_mode, "source-only")
        self.assertEqual(payload["status"], "dry-run")

    def test_managed_execution_is_rejected_before_any_source_operation(self):
        args = SimpleNamespace(execution_id="owned-execution", host="192.0.2.10")
        with mock.patch.object(adapters, "run_json_command") as run:
            self.assertEqual(adapters.sync_plan(args, mode="auto")["status"], "blocked")
            self.assertEqual(adapters.sync_apply(args, mode="auto")["status"], "blocked")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
