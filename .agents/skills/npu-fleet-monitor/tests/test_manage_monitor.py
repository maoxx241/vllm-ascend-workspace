from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/manage_monitor.py"
SPEC = importlib.util.spec_from_file_location("npu_fleet_monitor_manager", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def namespace(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "port": MODULE.DEFAULT_PORT,
        "wait_seconds": 0.5,
        "inventory_files": None,
        "host_pool_files": None,
        "bootstrap_command": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class FakeProcess:
    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


class ConstantsTests(unittest.TestCase):
    def test_spec_is_the_pinned_git_https_tag(self) -> None:
        self.assertEqual(MODULE.VAWS_TOP_REPO, "vllm-ascend-workspace/vaws-top")
        self.assertEqual(MODULE.VAWS_TOP_SPEC, "git+https://github.com/" + MODULE.VAWS_TOP_REPO + "@" + MODULE.VAWS_TOP_REF)
        self.assertTrue(MODULE.VAWS_TOP_REF.startswith("v"))
        self.assertEqual(MODULE.VAWS_TOP_COMMAND, MODULE.VAWS_TOP_REPO.rsplit("/", 1)[-1])
        self.assertEqual(MODULE.UVX_PREFIX, ["uvx", "--from", MODULE.VAWS_TOP_SPEC, MODULE.VAWS_TOP_COMMAND])

    def test_listener_is_loopback_only(self) -> None:
        self.assertEqual(MODULE.BIND, "127.0.0.1")
        self.assertEqual(MODULE.DEFAULT_PORT, 8788)
        self.assertEqual(MODULE.health_url(8788), "http://127.0.0.1:8788/api/health")
        command = MODULE.serve_command(9001)
        self.assertEqual(command[: len(MODULE.UVX_PREFIX)], MODULE.UVX_PREFIX)
        self.assertEqual(command[len(MODULE.UVX_PREFIX):], ["serve", "--bind", "127.0.0.1", "--port", "9001"])

    def test_source_has_no_checkout_pin_or_service_manager_paths(self) -> None:
        # Substring prefixes of the retired dependency-plane module, pin directory,
        # user-service manager, and checkout/build tooling.
        text = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("vaws_dep", "agents/deps", "systemctl", "system" "d", "git clone", "user-service", "npm"):
            self.assertNotIn(forbidden, text)
        self.assertNotIn("vaws_dep", " ".join(sys.modules))

    def test_runtime_dir_lives_under_untracked_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = MODULE.runtime_dir(Path(root))
        self.assertEqual(base.parts[-2:], (MODULE.STATE_DIRNAME, MODULE.RUNTIME_DIRNAME))
        self.assertEqual(MODULE.STATE_DIRNAME, ".vaws-local")


class EnvironmentTests(unittest.TestCase):
    def test_serve_env_forces_loopback_port_and_state_dir(self) -> None:
        inherited = {"NFM_BIND": "0.0.0.0", "NFM_PORT": "1", "PATH": "/usr/bin"}
        with mock.patch.dict(os.environ, inherited, clear=True), tempfile.TemporaryDirectory() as root:
            state = Path(root) / "data"
            env = MODULE.serve_env(namespace(), 8790, state)
        self.assertEqual(env["NFM_BIND"], "127.0.0.1")
        self.assertEqual(env["NFM_PORT"], "8790")
        self.assertEqual(env["NFM_STATE_DIR"], str(state))
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_consumer_env_defaults_come_from_shared_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            env = MODULE.consumer_env(namespace(), repo_root=repo, inherited={})
            self.assertEqual(env["NFM_INVENTORY_FILES"], str(MODULE.shared_inventory_path(repo)))
            self.assertNotIn("NFM_HOST_POOL_FILES", env)
            self.assertIn("bootstrap-host-key", env["NFM_BOOTSTRAP_COMMAND"])
            self.assertIn("--password-stdin", env["NFM_BOOTSTRAP_COMMAND"])
            self.assertNotIn("{password}", env["NFM_BOOTSTRAP_COMMAND"])
            (repo / "hosts.txt").write_text("192.0.2.10\n", encoding="utf-8")
            env = MODULE.consumer_env(namespace(), repo_root=repo, inherited={})
            self.assertEqual(env["NFM_HOST_POOL_FILES"], str(repo.resolve() / "hosts.txt"))

    def test_consumer_env_precedence_is_flag_then_inherited_then_default(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            inherited = {"NFM_INVENTORY_FILES": "/from/env.json", "NFM_BOOTSTRAP_COMMAND": "env-cmd {host}"}
            env = MODULE.consumer_env(namespace(inventory_files="/flag/a.json"), repo_root=repo, inherited=inherited)
        self.assertEqual(env["NFM_INVENTORY_FILES"], "/flag/a.json")
        self.assertEqual(env["NFM_BOOTSTRAP_COMMAND"], "env-cmd {host}")

    def test_consumer_env_normalizes_path_lists_and_rejects_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            joined = os.pathsep.join(["/a.json", " ", "/b.json", ""])
            env = MODULE.consumer_env(namespace(inventory_files=joined), repo_root=repo, inherited={})
            self.assertEqual(env["NFM_INVENTORY_FILES"], os.pathsep.join(["/a.json", "/b.json"]))
            env = MODULE.consumer_env(namespace(host_pool_files=""), repo_root=repo, inherited={})
            self.assertNotIn("NFM_HOST_POOL_FILES", env)
            with self.assertRaisesRegex(MODULE.MonitorError, "newline"):
                MODULE.consumer_env(namespace(inventory_files="/a.json\n/b.json"), repo_root=repo, inherited={})
            with self.assertRaisesRegex(MODULE.MonitorError, "newline"):
                MODULE.consumer_env(namespace(bootstrap_command="x\ny"), repo_root=repo, inherited={})


class PidfileTests(unittest.TestCase):
    def test_read_pidfile_rejects_missing_or_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "serve.json"
            self.assertIsNone(MODULE.read_pidfile(path))
            path.write_text("not json", encoding="utf-8")
            self.assertIsNone(MODULE.read_pidfile(path))
            path.write_text(json.dumps({"pid": "12"}), encoding="utf-8")
            self.assertIsNone(MODULE.read_pidfile(path))
            path.write_text(json.dumps({"pid": 12, "port": 8788}), encoding="utf-8")
            self.assertEqual(MODULE.read_pidfile(path), {"pid": 12, "port": 8788})

    def test_pid_alive(self) -> None:
        self.assertTrue(MODULE.pid_alive(os.getpid()))
        self.assertFalse(MODULE.pid_alive(0))
        self.assertFalse(MODULE.pid_alive(-1))


class StartTests(unittest.TestCase):
    def test_start_writes_pidfile_and_waits_for_health(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root) / "monitor"
            fake = FakeProcess(pid=4242)
            with mock.patch.object(MODULE, "require_uvx", return_value="/usr/bin/uvx"), mock.patch.object(
                MODULE, "start_process", return_value=fake
            ) as start_process, mock.patch.object(
                MODULE, "health", return_value=(True, {"status": "ok"}, None)
            ) as health:
                result = MODULE.do_start(namespace(port=8790), base)
            command = start_process.call_args.args[0]
            env = start_process.call_args.kwargs["env"]
            self.assertEqual(command, MODULE.serve_command(8790))
            self.assertEqual(env["NFM_BIND"], "127.0.0.1")
            self.assertEqual(env["NFM_PORT"], "8790")
            self.assertEqual(env["NFM_STATE_DIR"], str(base / "data"))
            self.assertEqual(start_process.call_args.kwargs["cwd"], base)
            self.assertEqual(health.call_args.args[:2], (8790, 0.5))
            self.assertTrue(result["ok"])
            self.assertFalse(result["already_running"])
            record = json.loads((base / MODULE.PIDFILE_NAME).read_text(encoding="utf-8"))
            self.assertEqual(record["pid"], 4242)
            self.assertEqual(record["port"], 8790)
            self.assertEqual(record["spec"], MODULE.VAWS_TOP_SPEC)

    def test_start_is_idempotent_when_pid_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / MODULE.PIDFILE_NAME).write_text(json.dumps({"pid": os.getpid(), "port": 8791}), encoding="utf-8")
            with mock.patch.object(MODULE, "start_process") as start_process, mock.patch.object(
                MODULE, "health", return_value=(True, {"status": "ok"}, None)
            ):
                result = MODULE.do_start(namespace(port=8788), base)
            start_process.assert_not_called()
            self.assertTrue(result["already_running"])
            self.assertEqual(result["port"], 8791)
            self.assertEqual(result["pid"], os.getpid())

    def test_start_reports_early_exit_and_clears_pidfile(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root) / "monitor"
            base.mkdir()
            (base / MODULE.LOG_NAME).write_text("build failed\n", encoding="utf-8")
            fake = FakeProcess(pid=4243, returncode=1)
            with mock.patch.object(MODULE, "require_uvx", return_value="/usr/bin/uvx"), mock.patch.object(
                MODULE, "start_process", return_value=fake
            ), mock.patch.object(MODULE, "health", return_value=(False, None, "refused")):
                result = MODULE.do_start(namespace(), base)
            self.assertFalse(result["ok"])
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(result["log_tail"], "build failed")
            self.assertFalse((base / MODULE.PIDFILE_NAME).exists())

    def test_start_requires_uvx(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch.object(MODULE.shutil, "which", return_value=None):
            with self.assertRaisesRegex(MODULE.MonitorError, "uvx is not on PATH"):
                MODULE.do_start(namespace(), Path(root))


class StopAndStatusTests(unittest.TestCase):
    def test_stop_without_pidfile_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result = MODULE.do_stop(Path(root))
        self.assertTrue(result["ok"])
        self.assertFalse(result["stopped"])

    def test_stop_removes_stale_pidfile(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            pidfile = base / MODULE.PIDFILE_NAME
            pidfile.write_text(json.dumps({"pid": 999999, "port": 8788}), encoding="utf-8")
            with mock.patch.object(MODULE, "pid_alive", return_value=False):
                result = MODULE.do_stop(base)
        self.assertTrue(result["ok"])
        self.assertFalse(result["stopped"])
        self.assertFalse(pidfile.exists())

    def test_stop_terminates_the_process_group(self) -> None:
        # Like a real `start`, the sleeper is detached from this test process so that
        # nothing here has to reap it; `stop` observes it through the pidfile only.
        launcher = (
            "import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
            " start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
            " print(p.pid)"
        )
        pid = int(subprocess.run([sys.executable, "-c", launcher], check=True, stdout=subprocess.PIPE, text=True).stdout)
        try:
            self.assertTrue(MODULE.pid_alive(pid))
            with tempfile.TemporaryDirectory() as root:
                base = Path(root)
                (base / MODULE.PIDFILE_NAME).write_text(json.dumps({"pid": pid, "port": 8788}), encoding="utf-8")
                with redirect_stderr(io.StringIO()):
                    result = MODULE.do_stop(base, timeout=5)
                self.assertFalse((base / MODULE.PIDFILE_NAME).exists())
        finally:
            MODULE._signal_group(pid, MODULE.signal.SIGKILL)
        self.assertTrue(result["ok"])
        self.assertTrue(result["stopped"])
        self.assertEqual(result["pid"], pid)
        self.assertFalse(MODULE.pid_alive(pid))

    def test_status_prefers_the_recorded_port(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / MODULE.PIDFILE_NAME).write_text(json.dumps({"pid": os.getpid(), "port": 8795}), encoding="utf-8")
            with mock.patch.object(MODULE, "health", return_value=(True, {"status": "ok"}, None)) as health:
                result = MODULE.do_status(base, 8788)
            self.assertEqual(health.call_args.args[0], 8795)
            self.assertTrue(result["running"])
            self.assertEqual(result["port"], 8795)
            with mock.patch.object(MODULE, "health", return_value=(False, None, "refused")):
                result = MODULE.do_status(Path(root) / "absent", 8788)
            self.assertFalse(result["ok"])
            self.assertFalse(result["running"])
            self.assertIsNone(result["pid"])


class PayloadAndMainTests(unittest.TestCase):
    def test_payload_declares_loopback_and_no_allocation_authority(self) -> None:
        payload = MODULE.payload_for("status", Path("/tmp/x"), 8788, {"ok": True})
        self.assertFalse(payload["allocation_authority"])
        self.assertEqual(payload["url"], "http://127.0.0.1:8788")
        self.assertEqual(payload["bind"], "127.0.0.1")
        self.assertEqual(payload["ref"], MODULE.VAWS_TOP_REF)
        self.assertEqual(payload["cli_prefix"], MODULE.UVX_PREFIX)
        self.assertEqual(payload["mcp_command"], [*MODULE.UVX_PREFIX, "mcp"])
        self.assertEqual(payload["state_dir"], "/tmp/x/data")

    def test_main_reports_missing_uvx_as_json_error(self) -> None:
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            MODULE, "runtime_dir", return_value=Path(root)
        ), mock.patch.object(MODULE.shutil, "which", return_value=None), redirect_stdout(out):
            code = MODULE.main(["deploy"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["allocation_authority"])
        self.assertIn("uvx is not on PATH", payload["error"])

    def test_main_rejects_invalid_port(self) -> None:
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            MODULE, "runtime_dir", return_value=Path(root)
        ), redirect_stdout(out):
            code = MODULE.main(["status", "--port", "70000"])
        self.assertEqual(code, 1)
        self.assertIn("invalid port", json.loads(out.getvalue())["error"])

    def test_main_status_without_service_exits_nonzero_with_json(self) -> None:
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            MODULE, "runtime_dir", return_value=Path(root)
        ), mock.patch.object(MODULE, "health", return_value=(False, None, "refused")), redirect_stdout(out):
            code = MODULE.main(["status"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["running"])
        self.assertEqual(payload["health_error"], "refused")

    def test_main_restart_stops_then_starts(self) -> None:
        out = io.StringIO()
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            MODULE, "runtime_dir", return_value=Path(root)
        ), mock.patch.object(
            MODULE, "do_stop", side_effect=lambda base, **_: calls.append("stop") or {"ok": True, "stopped": True}
        ), mock.patch.object(
            MODULE, "do_start", side_effect=lambda args, base: calls.append("start") or {"ok": True, "port": 8788}
        ), redirect_stdout(out):
            code = MODULE.main(["restart"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["stop", "start"])
        self.assertEqual(json.loads(out.getvalue())["stopped_previous"], {"ok": True, "stopped": True})


if __name__ == "__main__":
    unittest.main()
