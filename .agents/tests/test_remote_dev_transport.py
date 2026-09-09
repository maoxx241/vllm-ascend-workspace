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
    proc = subprocess.run(
        [ssh, "-G", "-F", "/dev/null", *argv[1:]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"HOME": home, "PATH": os.environ.get("PATH", "")},
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


class V03ConsumerArgvTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
