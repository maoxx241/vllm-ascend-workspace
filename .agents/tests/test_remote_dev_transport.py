"""Composed SSH argv and mux-stream refusal for the remote-dev consumer."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_remote_dev as remote_dev  # noqa: E402

HOST = "192.0.2.10"
PORT = 46001
USER = "root"


def _require_openssh() -> str:
    path = shutil.which("ssh")
    if path is None:
        raise AssertionError(
            "OpenSSH ssh is required to parse composed argv with ssh -G; "
            "a skipped parser test is how options-after-destination shipped"
        )
    return path


def _effective_ssh_config(argv: list[str], home: str) -> dict[str, str]:
    ssh = _require_openssh()
    config = Path(home) / "empty-ssh-config"
    config.write_text("", encoding="utf-8")
    proc = subprocess.run(
        [ssh, "-G", "-F", str(config), *argv[1:]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "HOME": home},
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"ssh -G failed (rc={proc.returncode}): {(proc.stderr or '')[:2000]}"
        )
    parsed: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(" ")
        parsed[key.lower()] = value.strip()
    return parsed


class TransportArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        remote_dev.require_transport()
        self.endpoint = SimpleNamespace(host=HOST, port=PORT, user=USER)

    def test_short_command_argv_is_multiplexed_without_keepalive(self) -> None:
        argv = remote_dev.ssh_argv(self.endpoint, connect_timeout_s=15)
        joined = " ".join(argv)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", joined)
        self.assertIn("ConnectTimeout=15", joined)
        self.assertIn("-l", argv)
        self.assertIn(USER, argv)
        self.assertIn("-p", argv)
        self.assertIn(str(PORT), argv)
        self.assertIn(HOST, argv)
        if os.name == "nt":
            self.assertIn("ControlMaster=no", joined)
            self.assertIn("ControlPath=none", joined)
        else:
            self.assertIn("ControlMaster=auto", joined)
            self.assertNotIn("ControlMaster=no", joined)
            self.assertNotIn("ControlPath=none", joined)
        self.assertNotIn("ServerAliveInterval=", joined)

    def test_long_stream_argv_disables_mux_and_enables_keepalive(self) -> None:
        argv = remote_dev.ssh_argv(self.endpoint, long_stream=True, connect_timeout_s=15)
        joined = " ".join(argv)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("ControlMaster=no", joined)
        self.assertIn("ControlPath=none", joined)
        self.assertIn("ControlPersist=no", joined)
        self.assertIn("ServerAliveInterval=30", joined)
        self.assertIn("ServerAliveCountMax=10", joined)
        self.assertIn("ConnectTimeout=15", joined)
        self.assertIn(HOST, argv)
        self.assertIn(str(PORT), argv)


class MuxedStreamRefusalTests(unittest.TestCase):
    def test_run_stream_refuses_a_muxed_endpoint(self) -> None:
        api = remote_dev.require_transport()
        endpoint = remote_dev.as_endpoint(HOST, PORT, USER, ssh_mux=True)
        with self.assertRaises(api["RemoteExecutionError"]) as ctx:
            api["run_stream"](endpoint, "true")
        message = str(ctx.exception).lower()
        self.assertTrue(
            "mux" in message or "controlmaster" in message or "stream" in message,
            ctx.exception,
        )

    def test_local_forward_refuses_a_muxed_endpoint(self) -> None:
        api = remote_dev.require_transport()
        endpoint = remote_dev.as_endpoint(HOST, PORT, USER, ssh_mux=True)
        with self.assertRaises(api["RemoteExecutionError"]):
            api["local_forward_ssh_command"](
                endpoint,
                local_host="127.0.0.1",
                local_port=47001,
                remote_host="127.0.0.1",
                remote_port=8000,
            )

    def test_interactive_refuses_a_muxed_endpoint(self) -> None:
        api = remote_dev.require_transport()
        endpoint = remote_dev.as_endpoint(HOST, PORT, USER, ssh_mux=True)
        with self.assertRaises(api["RemoteExecutionError"]):
            api["interactive_ssh_command"](endpoint, ["true"])


class ConsumerArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        remote_dev.require_transport()
        self.endpoint = SimpleNamespace(host=HOST, port=PORT, user=USER)

    def test_local_forward_argv_is_independent_with_exit_on_forward_failure(self) -> None:
        argv = remote_dev.local_forward_ssh_command(
            self.endpoint,
            local_host="127.0.0.1",
            local_port=34567,
            remote_host="127.0.0.1",
            remote_port=8000,
        )
        sep = argv.index("--")
        self.assertLess(argv.index("ExitOnForwardFailure=yes"), sep)
        self.assertLess(argv.index("-N"), sep)
        self.assertLess(argv.index("-L"), sep)
        self.assertEqual(argv[sep + 1 :], [HOST])
        with tempfile.TemporaryDirectory() as home:
            cfg = _effective_ssh_config(argv, home)
        self.assertEqual(cfg.get("exitonforwardfailure", "").lower(), "yes")
        self.assertIn(cfg.get("controlmaster", "").lower(), {"false", "no"})
        self.assertEqual(cfg.get("sessiontype", "").lower(), "none")
        self.assertEqual(cfg.get("serveraliveinterval"), "30")

    def test_interactive_argv_is_password_bootstrap_off_the_mux(self) -> None:
        argv = remote_dev.interactive_ssh_command(
            self.endpoint, ["sh", "-c", "true"], connect_timeout_s=10
        )
        joined = " ".join(argv)
        self.assertIn("BatchMode=no", joined)
        self.assertNotIn("BatchMode=yes", joined)
        self.assertIn("PubkeyAuthentication=no", joined)
        self.assertEqual(argv[argv.index("--") + 1 :], [HOST, "sh", "-c", "true"])
        with tempfile.TemporaryDirectory() as home:
            cfg = _effective_ssh_config(argv, home)
        self.assertEqual(cfg.get("batchmode", "").lower(), "no")
        self.assertIn(cfg.get("controlmaster", "").lower(), {"false", "no"})
        self.assertIn(cfg.get("pubkeyauthentication", "").lower(), {"false", "no"})
        self.assertEqual(cfg.get("numberofpasswordprompts"), "1")


class EndpointPolicyTests(unittest.TestCase):
    def test_mapping_endpoint_retains_authentication_and_execution_policy(self):
        from dataclasses import asdict
        from vaws_remote_target import ssh_endpoint_from_mapping

        mapping = dict(host=HOST, port=PORT, user="worker", identity_file="selected-key",
                       root="/bound-root", cwd="/case", runtime_env=False,
                       runtime_env_file="/runtime/custom.sh", connect_timeout_ms=4000,
                       ssh_mux=False, keepalive=True, kind="direct-endpoint",
                       alias="selected", source={"execution": "bound"})
        endpoint = ssh_endpoint_from_mapping(mapping)
        self.assertEqual(asdict(remote_dev.endpoint_from(endpoint)), mapping)
        argv = remote_dev.ssh_argv(endpoint)
        self.assertEqual(argv[argv.index("-i") + 1], "selected-key")
        self.assertEqual(argv[argv.index("-l") + 1], "worker")
        self.assertIn("ConnectTimeout=4", argv)

    def test_native_endpoint_overrides_preserve_routing_and_runtime_policy(self):
        Endpoint = remote_dev.require_transport()["Endpoint"]
        original = Endpoint(HOST, PORT, root="/bound-root", cwd="/old-cwd",
                            runtime_env=False, runtime_env_file="/custom-runtime.sh",
                            ssh_mux=True, identity_file="old-key", alias="worker",
                            source={"execution": "fixed-input"})
        selected = remote_dev.endpoint_from(original, connect_timeout_s=3, cwd="/selected-cwd",
                                            identity_file="selected-key", ssh_mux=False)
        self.assertEqual(selected.connect_timeout_ms, 3000)
        self.assertEqual(selected.cwd, "/selected-cwd")
        self.assertEqual(selected.identity_file, "selected-key")
        self.assertFalse(selected.ssh_mux)
        stream = remote_dev.endpoint_from(original, long_stream=True, connect_timeout_s=7)
        self.assertEqual(stream.connect_timeout_ms, 7000)
        self.assertFalse(stream.ssh_mux)
        self.assertTrue(stream.keepalive)
        for name in ("host", "port", "user", "root", "cwd", "runtime_env", "runtime_env_file", "identity_file", "alias", "source"):
            self.assertEqual(getattr(stream, name), getattr(original, name), name)
        self.assertTrue(original.ssh_mux)

    def test_interactive_wrapper_disables_mux_on_a_native_endpoint(self):
        endpoint = remote_dev.as_endpoint(HOST, PORT, ssh_mux=True)
        argv = remote_dev.interactive_ssh_command(endpoint, ["true"], connect_timeout_s=4)
        self.assertIn("ConnectTimeout=4", argv)
        self.assertIn("ControlMaster=no", argv)
        self.assertTrue(endpoint.ssh_mux)

    def test_transport_calls_do_not_inspect_dependency_lock(self):
        with mock.patch.object(remote_dev, "inspect", side_effect=AssertionError("ordinary I/O must not scan the lock")):
            argv = remote_dev.ssh_argv(SimpleNamespace(host=HOST, port=PORT, user=USER))
        self.assertIn(HOST, argv)

    def test_in_process_environment_removes_a_stale_resolver(self):
        with mock.patch.dict(os.environ, {"REMOTE_DEV_RESOLVERS": "obsolete:setup"}):
            remote_dev.apply_consumer_environment()
            self.assertNotIn("REMOTE_DEV_RESOLVERS", os.environ)

    def test_missing_exit_status_is_not_success_and_timeout_keeps_evidence(self):
        api = remote_dev.require_transport()
        endpoint = api["Endpoint"](HOST, PORT)
        completed = SimpleNamespace(returncode=None, stdout="partial output", stderr="connection evidence", timed_out=False)
        fake = {**api, "run_script": mock.Mock(return_value=completed), "run_stream": mock.Mock(return_value=completed)}
        with mock.patch.object(remote_dev, "require_transport", return_value=fake):
            result = remote_dev.ssh_exec(endpoint, "true", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "partial output")
            self.assertIn("connection evidence", result.stderr)
            self.assertIn("without an exit status", result.stderr)
            with self.assertRaisesRegex(RuntimeError, "without an exit status"):
                remote_dev.ssh_stream(endpoint, "true")
            completed.timed_out = True
            timed_out = remote_dev.ssh_exec(endpoint, "true", timeout=2, check=False)
            self.assertIn("connection evidence", timed_out.stderr)
            self.assertIn("timed out after 2s", timed_out.stderr)


if __name__ == "__main__":
    unittest.main()
