from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import socket
import signal
import subprocess
import sys
import tempfile
import time
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
        "spec": None,
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
    def test_default_spec_is_the_release_wheel_derived_from_one_tag(self) -> None:
        repo = MODULE.VAWS_TOP_REPO
        ref = MODULE.VAWS_TOP_REF
        self.assertEqual(repo, "vllm-ascend-workspace/vaws-top")
        self.assertTrue(ref.startswith("v"))
        self.assertEqual(MODULE.VAWS_TOP_VERSION, ref[1:])
        self.assertEqual(MODULE.VAWS_TOP_COMMAND, repo.rsplit("/", 1)[-1])
        self.assertEqual(MODULE.VAWS_TOP_PACKAGE, MODULE.VAWS_TOP_COMMAND.replace("-", "_"))
        self.assertEqual(MODULE.VAWS_TOP_WHEEL, f"{MODULE.VAWS_TOP_PACKAGE}-{ref[1:]}-py3-none-any.whl")
        self.assertEqual(
            MODULE.DEFAULT_VAWS_TOP_SPEC,
            f"https://github.com/{repo}/releases/download/{ref}/{MODULE.VAWS_TOP_WHEEL}",
        )
        # The frontend build only exists in the release wheel; a git+ default would
        # silently install a frontend-less package on hosts without Node.js.
        self.assertFalse(MODULE.DEFAULT_VAWS_TOP_SPEC.startswith("git+"))
        self.assertEqual(MODULE.uvx_prefix("SPEC"), ["uvx", "--from", "SPEC", MODULE.VAWS_TOP_COMMAND])

    def test_spec_override_is_flag_then_env_then_wheel(self) -> None:
        self.assertEqual(MODULE.resolve_spec(None, {}), MODULE.DEFAULT_VAWS_TOP_SPEC)
        self.assertEqual(MODULE.resolve_spec(None, {MODULE.SPEC_ENV: "/tmp/local.whl"}), "/tmp/local.whl")
        self.assertEqual(MODULE.resolve_spec("git+https://example.invalid/x@v1", {MODULE.SPEC_ENV: "/tmp/local.whl"}), "git+https://example.invalid/x@v1")
        self.assertEqual(MODULE.resolve_spec("  /tmp/tree  ", {}), "/tmp/tree")
        self.assertEqual(MODULE.SPEC_ENV, "VAWS_TOP_FROM")
        for bad in ("", "   ", "a b", "x\ny"):
            with self.subTest(bad=bad), self.assertRaisesRegex(MODULE.MonitorError, "invalid monitor install spec"):
                MODULE.resolve_spec(bad, {})

    def test_listener_is_loopback_only(self) -> None:
        self.assertEqual(MODULE.BIND, "127.0.0.1")
        self.assertEqual(MODULE.DEFAULT_PORT, 8788)
        self.assertEqual(MODULE.health_url(8788), "http://127.0.0.1:8788/api/health")
        prefix = MODULE.uvx_prefix(MODULE.DEFAULT_VAWS_TOP_SPEC)
        command = MODULE.serve_command(MODULE.DEFAULT_VAWS_TOP_SPEC, 9001)
        self.assertEqual(command[: len(prefix)], prefix)
        self.assertEqual(command[len(prefix):], ["serve", "--bind", "127.0.0.1", "--port", "9001"])

    def test_source_has_no_checkout_pin_or_service_manager_paths(self) -> None:
        # Substring prefixes of the retired dependency-plane module, pin directory,
        # user-service manager, and checkout/build tooling.
        text = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("vaws_dep", "agents/deps", "systemctl", "system" "d", "git clone", "user-service", "npm"):
            self.assertNotIn(forbidden, text)

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
            self.assertNotIn("NFM_BOOTSTRAP_COMMAND", env)
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

    def test_consumer_env_reuses_registered_coordinator_inventory(self) -> None:
        from vaws_coordinator.machine_directory import MACHINES_FILENAME
        from vaws_coordinator.state_paths import agent_sessions_root, coordinator_state_dir
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            inventory = coordinator_state_dir(agent_sessions_root(repo)) / MACHINES_FILENAME
            inventory.parent.mkdir(parents=True)
            inventory.write_text('{"machines": []}', encoding="utf-8")
            env = MODULE.consumer_env(namespace(), repo_root=repo, inherited={})
            self.assertEqual(env["NFM_INVENTORY_FILES"], str(inventory))

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
    def test_darwin_zombie_is_dead_even_when_signal_zero_would_succeed(self) -> None:
        for state, expected in (("Z", False), ("Z+", False), ("S+", True)):
            with self.subTest(state=state), mock.patch.object(MODULE.os, "name", "posix"), \
                 mock.patch.object(MODULE.sys, "platform", "darwin"), \
                 mock.patch.object(MODULE.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, state + "\n", "")), \
                 mock.patch.object(MODULE.os, "kill") as probe:
                self.assertEqual(MODULE.pid_alive(42), expected)
                if expected:
                    probe.assert_called_once_with(42, 0)
                else:
                    probe.assert_not_called()

    @unittest.skipIf(os.name == "nt", "native POSIX zombie regression")
    def test_native_ps_branch_identifies_unreaped_child_without_proc(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            with mock.patch.object(MODULE.sys, "platform", "darwin"):
                deadline = time.monotonic() + 5
                while MODULE.pid_alive(child.pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(MODULE.pid_alive(child.pid))
            # The kernel still knows the PID until this parent reaps it.
            os.kill(child.pid, 0)
        finally:
            child.wait(timeout=5)

    def test_darwin_identity_uses_native_birth_time_and_complete_command(self) -> None:
        import vaws_process_identity as identity
        result = subprocess.CompletedProcess([], 0, "S Sat Sep 12 10:20:30 2026 /path with spaces/python worker\n", "")
        with mock.patch.object(identity.os, "name", "posix"), mock.patch.object(identity.sys, "platform", "darwin"), \
             mock.patch.object(identity.subprocess, "run", return_value=result) as run:
            self.assertEqual(identity.process_identity(42), {"started": "Sat Sep 12 10:20:30 2026",
                             "command": "/path with spaces/python worker"})
        self.assertIn("-ww", run.call_args.args[0])

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


class DeployTests(unittest.TestCase):
    def _completed(self, code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["uvx"], code, out, err)

    def test_deploy_probes_the_packaged_frontend_inside_the_tool_env(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            index = Path(root) / "static" / "index.html"
            index.parent.mkdir()
            index.write_text("<!doctype html>", encoding="utf-8")
            with mock.patch.object(MODULE, "require_uvx", return_value="/usr/bin/uvx"), mock.patch.object(
                MODULE, "run_uvx", return_value=self._completed(0, f"0.1.0\n{index}\n")
            ) as run_uvx:
                result = MODULE.deploy("SPEC")
        self.assertEqual(run_uvx.call_args.args[0], "SPEC")
        self.assertEqual(run_uvx.call_args.args[1:3], ("python", "-c"))
        self.assertIn("require_static", run_uvx.call_args.args[3])
        self.assertEqual(result["version"], "0.1.0")
        self.assertEqual(result["static_index"], str(index))

    def test_deploy_fails_when_the_wheel_has_no_frontend(self) -> None:
        # This is exactly what a git+https install produces on a host without Node.js.
        message = "未找到打包的前端静态资源（vaws_top/static/index.html）"
        with mock.patch.object(MODULE, "require_uvx", return_value="/usr/bin/uvx"), mock.patch.object(
            MODULE, "run_uvx", return_value=self._completed(1, "", "Built pkg\n" + message)
        ):
            with self.assertRaisesRegex(MODULE.MonitorError, "is not servable") as ctx:
                MODULE.deploy("git+https://example.invalid/x@v1")
        self.assertIn(message, str(ctx.exception))
        self.assertIn("Release wheel", str(ctx.exception))
        self.assertIn("Node.js", str(ctx.exception))

    def test_deploy_fails_when_the_reported_index_is_missing(self) -> None:
        with mock.patch.object(MODULE, "require_uvx", return_value="/usr/bin/uvx"), mock.patch.object(
            MODULE, "run_uvx", return_value=self._completed(0, "0.1.0\n/nonexistent/static/index.html\n")
        ):
            with self.assertRaisesRegex(MODULE.MonitorError, "without the packaged frontend"):
                MODULE.deploy("SPEC")


class StartTests(unittest.TestCase):
    def test_start_rejects_an_existing_listener_without_claiming_its_health(self):
        with socket.socket() as listener, tempfile.TemporaryDirectory() as tmp:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with mock.patch.object(MODULE, "start_process") as start, mock.patch.object(MODULE, "health") as health:
                result = MODULE.do_start(namespace(port=listener.getsockname()[1]), Path(tmp), "SPEC")
            self.assertFalse(result["ok"])
            self.assertIn("already listening", result["detail"])
            start.assert_not_called()
            health.assert_not_called()

    def test_start_writes_pidfile_and_waits_for_health(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root) / "monitor"
            fake = FakeProcess(pid=4242)
            with mock.patch.object(MODULE, "require_uvx", return_value="/usr/bin/uvx"), mock.patch.object(
                MODULE, "start_process", return_value=fake
            ) as start_process, mock.patch.object(
                MODULE, "health", return_value=(True, {"status": "ok"}, None)
            ) as health, mock.patch.object(MODULE, "process_identity", return_value={"started": "1", "command": "uvx vaws-top"}), \
                 mock.patch.object(MODULE, "port_is_listening", return_value=False):
                result = MODULE.do_start(namespace(port=8790), base, "SPEC")
            command = start_process.call_args.args[0]
            env = start_process.call_args.kwargs["env"]
            self.assertEqual(command, MODULE.serve_command("SPEC", 8790))
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
            self.assertEqual(record["spec"], "SPEC")

    def test_start_is_idempotent_when_pid_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / MODULE.PIDFILE_NAME).write_text(
                json.dumps({"pid": os.getpid(), "port": 8791, "spec": "RUNNING",
                            "identity": MODULE.process_identity(os.getpid())}), encoding="utf-8"
            )
            with mock.patch.object(MODULE, "start_process") as start_process, mock.patch.object(
                MODULE, "health", return_value=(True, {"status": "ok"}, None)
            ):
                result = MODULE.do_start(namespace(port=8788), base, "SPEC")
            start_process.assert_not_called()
            self.assertTrue(result["already_running"])
            self.assertEqual(result["port"], 8791)
            self.assertEqual(result["spec"], "RUNNING")
            self.assertEqual(result["pid"], os.getpid())

    def test_start_reports_early_exit_and_clears_pidfile(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root) / "monitor"
            base.mkdir()
            (base / MODULE.LOG_NAME).write_text("build failed\n", encoding="utf-8")
            fake = FakeProcess(pid=4243, returncode=1)
            with mock.patch.object(MODULE, "require_uvx", return_value="/usr/bin/uvx"), mock.patch.object(
                MODULE, "start_process", return_value=fake
            ), mock.patch.object(MODULE, "health", return_value=(False, None, "refused")), \
                 mock.patch.object(MODULE, "port_is_listening", return_value=False):
                result = MODULE.do_start(namespace(), base, "SPEC")
            self.assertFalse(result["ok"])
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(result["log_tail"], "build failed")
            self.assertFalse((base / MODULE.PIDFILE_NAME).exists())

    def test_start_requires_uvx(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch.object(MODULE.shutil, "which", return_value=None), \
             mock.patch.object(MODULE, "port_is_listening", return_value=False):
            with self.assertRaisesRegex(MODULE.MonitorError, "uvx is not on PATH"):
                MODULE.do_start(namespace(), Path(root), "SPEC")


class StopAndStatusTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX group termination regression")
    def test_leader_exit_with_term_ignoring_child_is_reported_as_remaining_group(self):
        child_code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
        parent_code = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c'," + repr(child_code) +
                       "],stdout=subprocess.PIPE,text=True); p.stdout.readline(); print(p.pid,flush=True); time.sleep(60)")
        parent = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE,
                                  text=True, start_new_session=True)
        child_pid = int(parent.stdout.readline())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                record = {"pid": parent.pid, "identity": MODULE.process_identity(parent.pid)}
                (base / MODULE.PIDFILE_NAME).write_text(json.dumps(record), encoding="utf-8")
                result = MODULE.do_stop(base, timeout=0.5)
                self.assertFalse(result["ok"])
                self.assertTrue(result["group_remaining"])
                self.assertTrue((base / MODULE.PIDFILE_NAME).exists())
                self.assertTrue(MODULE.pid_alive(child_pid))
        finally:
            os.kill(child_pid, signal.SIGKILL)
            parent.wait(timeout=5)
            parent.stdout.close()

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
                (base / MODULE.PIDFILE_NAME).write_text(json.dumps({"pid": pid, "port": 8788,
                    "identity": MODULE.process_identity(pid)}), encoding="utf-8")
                with redirect_stderr(io.StringIO()):
                    result = MODULE.do_stop(base, timeout=5)
                self.assertFalse((base / MODULE.PIDFILE_NAME).exists())
        finally:
            MODULE._stop_group(pid, force=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["stopped"])
        self.assertEqual(result["pid"], pid)
        self.assertFalse(MODULE.pid_alive(pid))

    def test_status_prefers_the_recorded_port(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / MODULE.PIDFILE_NAME).write_text(json.dumps({"pid": os.getpid(), "port": 8795,
                "identity": MODULE.process_identity(os.getpid())}), encoding="utf-8")
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

    def test_reused_or_legacy_pid_never_reuses_or_stops_unrelated_process(self) -> None:
        identity = MODULE.process_identity(os.getpid())
        self.assertIsNotNone(identity)
        for bad in (None, {**identity, "started": "another birth"}, {**identity, "command": "another command"}):
            with self.subTest(identity=bad), tempfile.TemporaryDirectory() as root:
                base = Path(root)
                (base / MODULE.PIDFILE_NAME).write_text(json.dumps({"pid": os.getpid(), "identity": bad}), encoding="utf-8")
                with mock.patch.object(MODULE, "_stop_group") as stop, mock.patch.object(MODULE, "start_process") as start, \
                     mock.patch.object(MODULE, "health", return_value=(True, {"status": "ok"}, None)), \
                     mock.patch.object(MODULE, "runtime_dir", return_value=base), redirect_stdout(io.StringIO()):
                    self.assertFalse(MODULE.do_start(namespace(), base, "SPEC")["ok"])
                    self.assertFalse(MODULE.do_status(base, 8788)["running"])
                    self.assertFalse(MODULE.do_stop(base)["ok"])
                    self.assertEqual(MODULE.main(["restart"]), 1)
                stop.assert_not_called()
                start.assert_not_called()
                self.assertTrue(MODULE.pid_alive(os.getpid()))


class PayloadAndMainTests(unittest.TestCase):
    def test_payload_declares_loopback_and_no_allocation_authority(self) -> None:
        spec = MODULE.DEFAULT_VAWS_TOP_SPEC
        payload = MODULE.payload_for("status", Path("/tmp/x"), 8788, spec, {"ok": True})
        self.assertFalse(payload["allocation_authority"])
        self.assertEqual(payload["url"], "http://127.0.0.1:8788")
        self.assertEqual(payload["bind"], "127.0.0.1")
        self.assertEqual(payload["ref"], MODULE.VAWS_TOP_REF)
        self.assertEqual(payload["spec"], spec)
        self.assertEqual(payload["default_spec"], spec)
        self.assertEqual(payload["cli_prefix"], MODULE.uvx_prefix(spec))
        self.assertEqual(payload["mcp_command"], [*MODULE.uvx_prefix(spec), "mcp"])
        self.assertEqual(payload["state_dir"], str(Path("/tmp/x/data")))
        # A running instance's recorded spec wins over the resolved one.
        payload = MODULE.payload_for("status", Path("/tmp/x"), 8788, spec, {"ok": True, "spec": "RUNNING"})
        self.assertEqual(payload["spec"], "RUNNING")
        self.assertEqual(payload["cli_prefix"][2], "RUNNING")

    def test_main_passes_the_override_spec_to_deploy_and_start(self) -> None:
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            MODULE, "runtime_dir", return_value=Path(root)
        ), mock.patch.object(MODULE, "deploy", return_value={"version": "0.1.0"}) as deploy, redirect_stdout(out):
            code = MODULE.main(["deploy", "--from", "/tmp/dev.whl"])
        self.assertEqual(code, 0)
        deploy.assert_called_once_with("/tmp/dev.whl")
        self.assertEqual(json.loads(out.getvalue())["spec"], "/tmp/dev.whl")
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            MODULE, "runtime_dir", return_value=Path(root)
        ), mock.patch.dict(os.environ, {MODULE.SPEC_ENV: "/tmp/env.whl"}), mock.patch.object(
            MODULE, "do_start", return_value={"ok": True, "port": 8788}
        ) as do_start, redirect_stdout(out):
            code = MODULE.main(["start"])
        self.assertEqual(code, 0)
        self.assertEqual(do_start.call_args.args[2], "/tmp/env.whl")

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
            MODULE, "do_start", side_effect=lambda args, base, spec: calls.append("start") or {"ok": True, "port": 8788}
        ), redirect_stdout(out):
            code = MODULE.main(["restart"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["stop", "start"])
        self.assertEqual(json.loads(out.getvalue())["stopped_previous"], {"ok": True, "stopped": True})


if __name__ == "__main__":
    unittest.main()
