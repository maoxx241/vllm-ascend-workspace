"""Composed SSH argv and mux-stream refusal for the remote-dev consumer."""
from __future__ import annotations

import sys
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


if __name__ == "__main__":
    unittest.main()
