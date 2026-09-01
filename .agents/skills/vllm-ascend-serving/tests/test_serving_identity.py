#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from serve_start import service_runtime_dir  # noqa: E402
import _common
import serve_start
import serve_stop
import serve_status

A3_DEVICES = """
| 0     Ascend910  | OK | 170 | 48 | 0 / 0 |
| 0     0         | 0000:9D:00.0 | 0 | 0 / 0 | 3364 / 65536 |
| 0     Ascend910  | OK | -   | 50 | 0 / 0 |
| 1     1         | 0000:9F:00.0 | 0 | 0 / 0 | 2951 / 65536 |
| 2     Ascend910  | OK | 170 | 48 | 0 / 0 |
| 0     8         | 0000:89:00.0 | 0 | 0 / 0 | 3000 / 65536 |
"""
A3_HEADER = '| NPU Chip | Process id | Process name | Process memory(MB) |\n'
A3_ROWS = '| 0 0 | 4321 | python3 | 123 |\n| 0 1 | 4322 | python3 | 122 |\n| 2 0 | 4323 | worker | 120 |\n'


class ServingNpuProbeTests(unittest.TestCase):
    def probe(self, output):
        with mock.patch.object(_common, "ssh_exec", return_value=SimpleNamespace(returncode=0, stdout=output, stderr="")):
            return _common.probe_npus(object())

    def test_a3_process_rows_map_through_phy_id_below_hbm_threshold(self):
        parsed = self.probe(A3_DEVICES + A3_HEADER + A3_ROWS)
        self.assertEqual({key: value[0]["pid"] for key, value in parsed["busy"].items()},
                         {"0": 4321, "1": 4322, "8": 4323})
        self.assertEqual(parsed["free"], [])
        self.assertEqual(parsed["total"], 3)
        self.assertEqual(parsed["free_count"], 0)

    def test_missing_or_unparsable_process_table_fails_closed(self):
        for bad in (A3_DEVICES, A3_DEVICES + A3_HEADER + '| 0 9 | 4321 | python3 | 123 |\n'):
            with self.subTest(output=bad), self.assertRaisesRegex(RuntimeError, "parse failed"):
                self.probe(bad)


class ServingPresetTests(unittest.TestCase):
    def test_classify_stage_maps_runtime_log_markers(self):
        cases = {
            "Loading weights into memory": "weight-load",
            "Capturing ACL graphs for decode sizes": "graph-capture",
            "torch.compile init done": "compile",
            "Uvicorn running on http://0.0.0.0:30001": "http-up",
            "unrelated log line": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(serve_start.classify_stage(text), expected)

    def test_shipped_dsv4_flash_preset_is_valid_and_pinned(self):
        preset = serve_start.load_preset("dsv4-flash")
        self.assertEqual(preset["vllm_version"], "0.26.0")
        self.assertEqual(preset["tp"], 8)
        self.assertIn("--quantization", preset["serve_args"])
        import json as _json
        for flag in serve_start._JSON_VALUE_FLAGS:
            if flag in preset["serve_args"]:
                idx = preset["serve_args"].index(flag)
                _json.loads(preset["serve_args"][idx + 1])

    def preflight(self, preset, *, grep_rc=0, grep_out="", missing=(), args=None):
        def fake_ssh(ep, script, *, check=True):
            if script.startswith("grep -m1 '^__version__ = '"):
                return SimpleNamespace(returncode=grep_rc, stdout=grep_out, stderr="")
            if script.startswith("test -d "):
                path = script[len("test -d "):].strip("'\"")
                return SimpleNamespace(returncode=1 if path in missing else 0, stdout="", stderr="")
            raise AssertionError(f"unexpected ssh_exec: {script}")
        with mock.patch.object(serve_start, "ssh_exec", side_effect=fake_ssh):
            return serve_start.preflight_preset(
                object(), preset, runtime_base="/vllm-workspace",
                env={"PYTHONPATH": "/a:/b"}, extra_args=args or [],
            )

    def test_preflight_version_mismatch_blocks_launch(self):
        problems = self.preflight(
            {"vllm_version": "0.26.0"},
            grep_out="__version__ = version = '0.27.1'\n",
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("0.26.0", problems[0])
        self.assertIn("0.27.1", problems[0])

    def test_preflight_missing_pythonpath_and_bad_json(self):
        problems = self.preflight(
            {},
            missing=("/b",),
            args=["--additional-config", "{not json}"],
        )
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("/b" in p for p in problems))
        self.assertTrue(any("--additional-config" in p for p in problems))

    def test_preflight_passes_clean_recipe(self):
        self.assertEqual(self.preflight(
            {"vllm_version": "0.26.0"},
            grep_out="__version__ = version = '0.26.0'\n",
            args=["--additional-config", '{"a": true}'],
        ), [])

    def test_preflight_catches_every_bad_json_flag_occurrence(self):
        problems = self.preflight({}, args=[
            "--additional-config", '{"ok": true}',
            "--additional-config", "{bad}",
        ])
        self.assertEqual(len(problems), 1)
        self.assertIn("--additional-config", problems[0])

    def test_preflight_covers_flag_equals_value_form(self):
        problems = self.preflight({}, args=[
            '--compilation-config={"level":0}',
            '--speculative-config={bad}',
        ])
        self.assertEqual(len(problems), 1)
        self.assertIn("--speculative-config", problems[0])

    def test_preflight_flag_without_value(self):
        problems = self.preflight({}, args=["--additional-config"])
        self.assertEqual(len(problems), 1)
        self.assertIn("has no value", problems[0])


class ServingReadinessTests(unittest.TestCase):
    def run_wait(self, probes, token_results, timeout=25):
        state = {"t": 0.0}
        with mock.patch.object(serve_start.time, "monotonic", side_effect=lambda: state["t"]), \
             mock.patch.object(serve_start.time, "sleep", side_effect=lambda _: state.__setitem__("t", state["t"] + 10)), \
             mock.patch.object(serve_start, "probe_ready_once", side_effect=probes), \
             mock.patch.object(serve_start, "probe_first_token", side_effect=token_results), \
             mock.patch.object(serve_start, "read_remote_tail", return_value=""), \
             mock.patch.object(serve_start, "check_alive", return_value=True):
            return serve_start.wait_for_ready(object(), 1, 8000, "/rd", timeout, "model-x")

    def good_probe(self, **kw):
        probe = {"alive": True, "health": True, "models": {"data": [{"id": "model-x"}]},
                 "stage": None, "probe_error": False}
        probe.update(kw)
        return probe

    def test_ready_requires_first_token_and_records_phases(self):
        result = self.run_wait(
            [self.good_probe(stage="weight-load", health=False, models=None),
             self.good_probe(stage="graph-capture")],
            [{"ok": True, "probe_error": False, "detail": ""}],
        )
        self.assertTrue(result["ready"])
        names = [p["phase"] for p in result["phases"]]
        self.assertEqual(names, ["weight-load", "graph-capture", "health-ok", "models-ok", "first-token-ok"])

    def test_ssh_probe_error_is_not_a_dead_process(self):
        result = self.run_wait(
            [{"alive": False, "health": False, "models": None, "stage": None, "probe_error": True},
             self.good_probe()],
            [{"ok": True, "probe_error": False, "detail": ""}],
        )
        self.assertTrue(result["ready"])

    def test_first_token_failure_times_out_with_phase_hint(self):
        result = self.run_wait(
            [self.good_probe()] * 5,
            [{"ok": False, "probe_error": False, "detail": "500"}] * 5,
        )
        self.assertFalse(result["ready"])
        self.assertIn("first-token-failing", result["error"])
        self.assertEqual(result["last_phase"], "first-token-failing")


class ServingIdentityTests(unittest.TestCase):
    def test_lost_ssh_is_not_a_dead_process_in_any_service_entrypoint(self):
        for module in (serve_start, serve_stop, serve_status):
            with self.subTest(module=module.__name__), mock.patch.object(module, "ssh_exec", return_value=SimpleNamespace(returncode=255, stdout="")):
                with self.assertRaisesRegex(RuntimeError, "unknown"):
                    module.check_alive(object(), 123)

    def test_stop_does_not_release_ports_on_unknown_process_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = SimpleNamespace(alias="test", endpoint=object(), session_id="session-test", state_repo_root=Path(tmp))
            with mock.patch.object(serve_stop, "resolve_execution_target", return_value=target), \
                 mock.patch.object(serve_stop, "file_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(serve_stop, "load_serving_state", return_value={"pid": 123, "port": 18000}), \
                 mock.patch.object(serve_stop, "ssh_exec", return_value=SimpleNamespace(returncode=255, stdout="")), \
                 mock.patch.object(serve_stop, "save_serving_state") as save, \
                 mock.patch.object(serve_stop, "release_service_port") as release, \
                 mock.patch.object(serve_stop, "print_json"):
                self.assertEqual(serve_stop.main(["--session-id", "session-test"]), 2)
                save.assert_not_called()
                release.assert_not_called()

    def test_source_only_staging_blocks_before_npu_probe_or_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = SimpleNamespace(record={}, alias="test", endpoint=object(), runtime_base="/workspace",
                                     session_id="session-test", session_file=None, session={}, state_repo_root=Path(tmp))
            with mock.patch.object(serve_start, "resolve_execution_target", return_value=target), \
                 mock.patch.object(serve_start, "file_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(serve_start, "require_session_npu_lease", return_value=[0]), \
                 mock.patch.object(serve_start, "load_serving_state", return_value=None), \
                 mock.patch.object(serve_start, "ssh_exec", return_value=SimpleNamespace(returncode=0)) as ssh, \
                 mock.patch.object(serve_start, "run_parity", return_value={"status": "source-only"}), \
                 mock.patch.object(serve_start, "probe_npus") as probe, \
                 mock.patch.object(serve_start, "print_json") as output:
                self.assertEqual(serve_start.main(["--model", "/models/test", "--tp", "1", "--devices", "0"]), 1)
                self.assertEqual(output.call_args.args[0]["status"], "blocked")
                probe.assert_not_called()
                self.assertEqual(ssh.call_count, 1)  # model-path existence only

    def test_host_npu_probe_failure_blocks_before_port_or_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = SimpleNamespace(
                record={}, alias="test", endpoint=object(), host_endpoint=object(),
                runtime_base="/workspace", mode="session", session_id="session-test",
                session_file=None, session={}, state_repo_root=Path(tmp),
            )
            with mock.patch.object(serve_start, "resolve_execution_target", return_value=target), \
                 mock.patch.object(serve_start, "file_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(serve_start, "require_session_npu_lease", return_value=[0]), \
                 mock.patch.object(serve_start, "load_serving_state", return_value=None), \
                 mock.patch.object(serve_start, "ssh_exec", return_value=SimpleNamespace(returncode=0)), \
                 mock.patch.object(serve_start, "probe_npus", side_effect=RuntimeError("npu-smi parse failed")), \
                 mock.patch.object(serve_start, "remote_port_availability") as port_probe, \
                 mock.patch.object(serve_start, "print_json") as output:
                rc = serve_start.main([
                    "--model", "/models/test", "--tp", "1", "--devices", "0",
                    "--skip-parity",
                ])

            self.assertEqual(rc, 1)
            self.assertEqual(output.call_args.args[0]["status"], "blocked")
            self.assertEqual(output.call_args.args[0]["phase"], "probe-npus")
            self.assertIn("occupancy could not be verified", output.call_args.args[0]["error"])
            port_probe.assert_not_called()

    def test_missing_device_is_not_a_successful_free_probe(self):
        with mock.patch.object(serve_start, "probe_npus", return_value={"devices": [], "busy": {}}):
            self.assertFalse(serve_start.wait_for_devices_free(object(), {0}, timeout=0))

    def test_unconfirmed_device_free_warns_but_launch_proceeds(self):
        prev_state = {"pid": 555, "port": 18000, "devices": "0"}
        with tempfile.TemporaryDirectory() as tmp:
            target = SimpleNamespace(
                record={}, alias="test",
                endpoint=SimpleNamespace(host="10.0.0.1", port=22),
                host_endpoint=SimpleNamespace(host="10.0.0.1", port=22),
                runtime_base="/workspace", mode="session", session_id="session-test",
                session_file=None, session={}, state_repo_root=Path(tmp),
            )

            def fake_ssh(ep, script, *, check=True, **kw):
                if script.startswith("test -d ") or "kill -2 555" in script:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="4242\n", stderr="")

            stderr = io.StringIO()
            with mock.patch.object(serve_start, "resolve_execution_target", return_value=target), \
                 mock.patch.object(serve_start, "file_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(serve_start, "require_session_npu_lease", return_value=[0]), \
                 mock.patch.object(serve_start, "load_serving_state", return_value=prev_state), \
                 mock.patch.object(serve_start, "check_alive", side_effect=[True, False, False, False]), \
                 mock.patch.object(serve_start, "wait_for_devices_free", return_value=False), \
                 mock.patch.object(serve_start, "ssh_exec", side_effect=fake_ssh), \
                 mock.patch.object(serve_start, "probe_npus", return_value={"devices": [0], "free": [0], "busy": {}}), \
                 mock.patch.object(serve_start, "allocate_service_port", return_value=8000), \
                 mock.patch.object(serve_start, "remote_port_availability", return_value=lambda candidate: True), \
                 mock.patch.object(serve_start, "remote_port_available", return_value=True), \
                 mock.patch.object(serve_start, "load_workspace_identity", return_value=None), \
                 mock.patch.object(serve_start, "effective_workspace_alias", return_value=None), \
                 mock.patch.object(serve_start, "wait_for_ready", return_value={"ready": True, "alive": True, "phases": [], "elapsed_seconds": 0.1}), \
                 mock.patch.object(serve_start, "save_serving_state"), \
                 mock.patch.object(serve_start, "release_service_port"), \
                 mock.patch.object(serve_start, "print_json") as output, \
                 contextlib.redirect_stderr(stderr):
                rc = serve_start.main(["--model", "/models/test", "--tp", "1", "--skip-parity"])

            self.assertEqual(rc, 0)
            self.assertEqual(output.call_args.args[0]["status"], "ready")
            self.assertIn("devices-may-still-be-busy", stderr.getvalue())

    def test_alias_namespaces_runtime_directory(self) -> None:
        self.assertEqual(
            service_runtime_dir("/vllm-workspace", "20260811_120000", "agent12345"),
            "/vllm-workspace/.vaws-runtime/serving/agent12345/20260811_120000",
        )

    def test_missing_alias_preserves_legacy_layout(self) -> None:
        self.assertEqual(
            service_runtime_dir("/vllm-workspace", "20260811_120000", None),
            "/vllm-workspace/.vaws-runtime/serving/20260811_120000",
        )


class LaunchScriptEscapingTests(unittest.TestCase):
    """A19: build_launch_script quoting and heredoc safety."""

    def build(self, **kw):
        params = dict(
            runtime_dir="/vllm-workspace/.vaws-runtime/serving/ts",
            model="/models/Qwen3-32B",
            served_model_name="qwen3",
            port=8000,
            tp=4, dp=None,
            devices="0,1,2,3",
            extra_env={},
            extra_args=[],
        )
        params.update(kw)
        return serve_start.build_launch_script(**params)

    def test_metacharacters_are_individually_quoted(self):
        model = "/models/My Model $(touch /tmp/pwned);`id`"
        served = 'evil";$(reboot);"'
        extra = ["--additional-config", '{"a": "b c"}', "--note=';rm -rf ~;'"]
        script = self.build(model=model, served_model_name=served, extra_args=extra)
        for token in (model, served, *extra):
            self.assertIn(shlex.quote(token), script)
        # The whole generated script must still parse as bash.
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(script)
            path = fh.name
        try:
            check = subprocess.run(["bash", "-n", path], capture_output=True, text=True, check=False)
            self.assertEqual(check.returncode, 0, check.stderr)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_newline_tokens_are_rejected_before_heredoc(self):
        for kw in (
            {"model": "/models/ok\nVAWS_SERVE_EOF\nrm -rf ~"},
            {"served_model_name": "x\nVAWS_SERVE_EOF"},
            {"extra_args": ["--additional-config", '{"a":1}\nVAWS_SERVE_EOF\nid']},
        ):
            with self.subTest(kw=kw), self.assertRaisesRegex(ValueError, "newline"):
                self.build(**kw)


class FailedLaunchCleanupTests(unittest.TestCase):
    """E2: a failed launch must not release the port while a process may live."""

    def fake_ssh(self, scripts, pid_file_rc=0, pid_file_out=""):
        def _fake(ep, script, *, check=True, **kw):
            scripts.append(script)
            if script.startswith("cat "):
                return SimpleNamespace(returncode=pid_file_rc, stdout=pid_file_out, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return _fake

    def test_cleanup_kills_pid_from_pid_file_and_stdout(self):
        scripts: list[str] = []
        with mock.patch.object(serve_start, "ssh_exec", side_effect=self.fake_ssh(scripts, pid_file_out="4321\n")), \
             mock.patch.object(serve_start, "check_alive", side_effect=[True, False]) as alive, \
             mock.patch.object(serve_start.time, "sleep"):
            self.assertTrue(serve_start.cleanup_failed_launch(object(), "/rd", "junk\n4321"))
        self.assertEqual(alive.call_args_list[0].args[1], 4321)
        self.assertTrue(any("kill -15 4321" in s for s in scripts))

    def test_cleanup_true_when_no_pid_anywhere(self):
        with mock.patch.object(serve_start, "ssh_exec", side_effect=self.fake_ssh([])):
            self.assertTrue(serve_start.cleanup_failed_launch(object(), "/rd", "not-a-pid"))

    def test_cleanup_unknown_when_pid_file_unreadable(self):
        with mock.patch.object(serve_start, "ssh_exec", side_effect=self.fake_ssh([], pid_file_rc=1)):
            self.assertFalse(serve_start.cleanup_failed_launch(object(), "/rd", ""))

    def test_cleanup_unknown_when_process_survives_or_state_unknown(self):
        with mock.patch.object(serve_start, "ssh_exec", side_effect=self.fake_ssh([])), \
             mock.patch.object(serve_start, "check_alive", return_value=True), \
             mock.patch.object(serve_start.time, "sleep"):
            self.assertFalse(serve_start.cleanup_failed_launch(object(), "/rd", "999"))
        with mock.patch.object(serve_start, "ssh_exec", side_effect=self.fake_ssh([])), \
             mock.patch.object(serve_start, "check_alive", side_effect=RuntimeError("unknown")), \
             mock.patch.object(serve_start.time, "sleep"):
            self.assertFalse(serve_start.cleanup_failed_launch(object(), "/rd", "999"))

    def test_abort_releases_port_only_when_cleanup_confirms(self):
        release_kwargs = {"repo_root": Path("/tmp/x"), "machine_alias": "m", "session_id": "s", "port": 8000}
        with mock.patch.object(serve_start, "cleanup_failed_launch", return_value=True), \
             mock.patch.object(serve_start, "release_service_port") as release, \
             mock.patch.object(serve_start, "print_json") as out:
            rc = serve_start.abort_failed_launch(
                ep=object(), runtime_dir="/rd", launch_stdout="",
                release_kwargs=release_kwargs, payload={"error": "boom"},
            )
        self.assertEqual(rc, 1)
        release.assert_called_once_with(**release_kwargs)
        self.assertEqual(out.call_args.args[0]["status"], "failed")

        with mock.patch.object(serve_start, "cleanup_failed_launch", return_value=False), \
             mock.patch.object(serve_start, "release_service_port") as release, \
             mock.patch.object(serve_start, "print_json") as out:
            rc = serve_start.abort_failed_launch(
                ep=object(), runtime_dir="/rd", launch_stdout="",
                release_kwargs=release_kwargs, payload={"error": "boom"},
            )
        self.assertEqual(rc, 1)
        release.assert_not_called()
        self.assertEqual(out.call_args.args[0]["status"], "needs_repair")
        self.assertIn("port lease was kept", out.call_args.args[0]["error"])


class ProbeErrorSemanticsTests(unittest.TestCase):
    """E4: any nonzero probe rc is unknown, never proof of process exit."""

    def test_nonzero_rc_is_unknown_even_with_alive_marker(self):
        torn = SimpleNamespace(returncode=255, stdout="__ALIVE__=0\n__HEALTH__=000\n", stderr="")
        with mock.patch.object(serve_start, "ssh_exec", return_value=torn):
            probe = serve_start.probe_ready_once(object(), 123, 8000)
        self.assertTrue(probe["probe_error"])
        self.assertFalse(probe["alive"])

    def test_zero_rc_dead_process_is_not_probe_error(self):
        out = "__ALIVE__=0\n__HEALTH__=000\n"
        with mock.patch.object(serve_start, "ssh_exec", return_value=SimpleNamespace(returncode=0, stdout=out, stderr="")):
            probe = serve_start.probe_ready_once(object(), 123, 8000)
        self.assertFalse(probe["probe_error"])
        self.assertFalse(probe["alive"])

    def test_first_token_nonzero_rc_is_unknown(self):
        with mock.patch.object(serve_start, "ssh_exec", return_value=SimpleNamespace(returncode=255, stdout="__CODE__=000", stderr="")):
            token = serve_start.probe_first_token(object(), 8000, "m")
        self.assertTrue(token["probe_error"])
        self.assertFalse(token["ok"])
        with mock.patch.object(serve_start, "ssh_exec", return_value=SimpleNamespace(returncode=0, stdout="__CODE__=200\n{}", stderr="")):
            token = serve_start.probe_first_token(object(), 8000, "m")
        self.assertFalse(token["probe_error"])
        self.assertTrue(token["ok"])

    def test_first_token_body_file_is_unique_per_remote_shell(self):
        captured: dict[str, str] = {}

        def fake_ssh(ep, script, *, check=True, **kw):
            captured["script"] = script
            return SimpleNamespace(returncode=0, stdout="__CODE__=200", stderr="")

        with mock.patch.object(serve_start, "ssh_exec", side_effect=fake_ssh):
            serve_start.probe_first_token(object(), 8000, "m")
        self.assertIn("/tmp/vaws_first_token.$$.json", captured["script"])
        self.assertNotIn("-o /tmp/vaws_first_token.json", captured["script"])


class SshTimeoutTests(unittest.TestCase):
    """E3: probe chain hard timeouts."""

    def endpoint(self):
        return _common.SshEndpoint(host="192.0.2.1", port=22)

    def test_ssh_exec_passes_connect_and_subprocess_timeouts(self):
        with mock.patch.object(
            _common.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ) as run:
            _common.ssh_exec(self.endpoint(), "true", check=False)
        self.assertEqual(run.call_args.kwargs["timeout"], _common.SSH_EXEC_DEFAULT_TIMEOUT_SECONDS)
        cmd = run.call_args.args[0]
        self.assertIn(f"ConnectTimeout={_common.SSH_CONNECT_TIMEOUT_SECONDS}", cmd)

    def test_ssh_exec_timeout_is_an_unknown_result(self):
        with mock.patch.object(
            _common.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=7),
        ):
            result = _common.ssh_exec(self.endpoint(), "true", check=False, timeout=7)
        self.assertEqual(result.returncode, 255)
        self.assertIn("timed out", result.stderr)
        with mock.patch.object(
            _common.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=7),
        ), self.assertRaisesRegex(RuntimeError, "rc=255"):
            _common.ssh_exec(self.endpoint(), "true", check=True, timeout=7)

    def test_run_parity_timeout_kills_child(self):
        class FakeProc:
            def __init__(self):
                self.stdout = iter(())
                self.stderr = iter(())
                self.killed = False

            def wait(self, timeout=None):
                if timeout is not None and not self.killed:
                    raise subprocess.TimeoutExpired(cmd="parity", timeout=timeout)
                return 0

            def kill(self):
                self.killed = True

        proc = FakeProc()
        with mock.patch.object(serve_start.subprocess, "Popen", return_value=proc):
            result = serve_start.run_parity("session-test")
        self.assertEqual(result["status"], "failed")
        self.assertIn("timed out", result["error"])
        self.assertTrue(proc.killed)

    def test_run_parity_success_still_parses_stdout(self):
        class FakeProc:
            def __init__(self):
                self.stdout = iter(['{"status": "ready"}\n'])
                self.stderr = iter(())

            def wait(self, timeout=None):
                return 0

            def kill(self):
                raise AssertionError("kill must not be called on success")

        with mock.patch.object(serve_start.subprocess, "Popen", return_value=FakeProc()):
            result = serve_start.run_parity("session-test")
        self.assertEqual(result, {"status": "ready"})


class RelaunchMergeUnsetTests(unittest.TestCase):
    """A22/A23: merge_with_previous unset rules."""

    PREV = {
        "model": "/models/A",
        "served_model_name": "a",
        "tp": 4,
        "dp": None,
        "devices": "0,1,2,3",
        "env": {"VLLM_LOGGING_LEVEL": "DEBUG"},
        "extra_args": ["--enforce-eager", "--max-model-len", "2048"],
    }

    def merge(self, previous=None, **kw):
        params = dict(
            model=None, served_model_name=None, tp=None, dp=None, devices=None,
            extra_env={}, unset_env=[], extra_args=[], unset_args=[],
        )
        params.update(kw)
        return serve_start.merge_with_previous(dict(previous or self.PREV), **params)

    def test_a22_unset_boolean_flag_preserves_value_flag(self):
        merged = self.merge(unset_args=["--enforce-eager"])
        self.assertEqual(merged["extra_args"], ["--max-model-len", "2048"])

    def test_a23_unset_value_flag_removes_its_value(self):
        merged = self.merge(unset_args=["--max-model-len"])
        self.assertEqual(merged["extra_args"], ["--enforce-eager"])

    def test_unset_equals_form_and_unset_env(self):
        prev = dict(self.PREV, extra_args=["--max-model-len=2048", "--enforce-eager"])
        merged = self.merge(
            prev,
            unset_args=["--max-model-len"],
            unset_env=["VLLM_LOGGING_LEVEL"],
        )
        self.assertEqual(merged["extra_args"], ["--enforce-eager"])
        self.assertEqual(merged["env"], {})


if __name__ == "__main__":
    unittest.main()
