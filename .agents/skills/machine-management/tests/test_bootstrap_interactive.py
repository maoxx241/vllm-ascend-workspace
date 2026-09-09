#!/usr/bin/env python3

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

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
SCRIPTS = ROOT / ".agents" / "skills" / "machine-management" / "scripts"
for value in (str(LIB_DIR), str(SCRIPTS)):
    if value not in sys.path:
        sys.path.insert(0, value)

import manage_machine as machine_ops  # noqa: E402
import vaws_remote_dev as remote_dev  # noqa: E402


def _effective_ssh_config(cmd: list[str], home: str) -> dict[str, list[str]]:
    ssh = shutil.which("ssh")
    if ssh is None:
        raise AssertionError(
            "ssh binary is required to parse interactive argv; a skipped "
            "parser test is how BatchMode=yes bootstrap survives"
        )
    result = subprocess.run(
        [ssh, "-G", "-F", "/dev/null", *cmd[1:]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"HOME": home, "PATH": os.environ.get("PATH", "")},
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"ssh -G failed (rc={result.returncode}): {(result.stderr or '')[:2000]}"
        )
    parsed: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(" ")
        parsed.setdefault(key.lower(), []).append(value)
    return parsed


class InteractiveBootstrapTests(unittest.TestCase):
    def test_bootstrap_argv_matches_package_interactive_command(self) -> None:
        target = machine_ops.SshTarget(host="192.0.2.10", user="ubuntu", port=22)
        _tool, command = machine_ops.build_bootstrap_host_key_command(
            target,
            key_path=Path("/tmp/id.pub"),
            public_key="ssh-ed25519 AAAA test",
        )
        expected = remote_dev.interactive_ssh_command(
            target,
            command[command.index("--") + 2 :],
            connect_timeout_s=10,
        )
        self.assertEqual(command, expected)
        self.assertIn("BatchMode=no", command)
        self.assertNotIn("BatchMode=yes", command)
        with tempfile.TemporaryDirectory() as home:
            cfg = _effective_ssh_config(command, home)
        self.assertEqual(cfg.get("batchmode"), ["no"])
        self.assertIn((cfg.get("controlmaster") or [""])[0].lower(), {"false", "no"})
        self.assertIn(
            (cfg.get("pubkeyauthentication") or [""])[0].lower(), {"false", "no"}
        )
        self.assertEqual(cfg.get("numberofpasswordprompts"), ["1"])

    def test_tty_path_calls_run_interactive_not_local_runner(self) -> None:
        args = SimpleNamespace(
            host="192.0.2.10",
            user="ubuntu",
            host_port=22,
            public_key_file=None,
            print_command=False,
        )
        with (
            mock.patch.object(machine_ops, "find_public_key", return_value=Path("/tmp/id.pub")),
            mock.patch.object(
                machine_ops, "private_key_for_public_key", return_value=Path("/tmp/id")
            ),
            mock.patch.object(machine_ops, "load_public_key", return_value="ssh-ed25519 AAAA"),
            mock.patch.object(
                machine_ops,
                "read_password_value",
                return_value=(None, None, machine_ops.DEFAULT_PASSWORD_ENV),
            ),
            mock.patch.object(
                machine_ops,
                "check_direct_ssh",
                side_effect=[
                    {"ok": False, "returncode": 255, "stdout": "", "stderr": "denied"},
                    {"ok": True, "returncode": 0, "stdout": "ok", "stderr": ""},
                ],
            ),
            mock.patch.object(machine_ops, "run_interactive", return_value=0) as interactive,
            mock.patch.object(machine_ops, "run_local_interactive") as local_runner,
            mock.patch.object(machine_ops, "run_with_askpass") as askpass,
            mock.patch.object(machine_ops, "print_json"),
        ):
            rc = machine_ops.cmd_bootstrap_host_key(args)
        self.assertEqual(rc, 0)
        interactive.assert_called_once()
        local_runner.assert_not_called()
        askpass.assert_not_called()

    def test_password_path_stays_on_askpass(self) -> None:
        args = SimpleNamespace(
            host="192.0.2.10",
            user="ubuntu",
            host_port=22,
            public_key_file=None,
            print_command=False,
        )
        with (
            mock.patch.object(machine_ops, "find_public_key", return_value=Path("/tmp/id.pub")),
            mock.patch.object(
                machine_ops, "private_key_for_public_key", return_value=Path("/tmp/id")
            ),
            mock.patch.object(machine_ops, "load_public_key", return_value="ssh-ed25519 AAAA"),
            mock.patch.object(
                machine_ops,
                "read_password_value",
                return_value=("secret", "literal", machine_ops.DEFAULT_PASSWORD_ENV),
            ),
            mock.patch.object(
                machine_ops,
                "check_direct_ssh",
                side_effect=[
                    {"ok": False, "returncode": 255, "stdout": "", "stderr": "denied"},
                    {"ok": True, "returncode": 0, "stdout": "ok", "stderr": ""},
                ],
            ),
            mock.patch.object(
                machine_ops,
                "run_with_askpass",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ) as askpass,
            mock.patch.object(machine_ops, "run_interactive") as interactive,
            mock.patch.object(machine_ops, "print_json"),
        ):
            rc = machine_ops.cmd_bootstrap_host_key(args)
        self.assertEqual(rc, 0)
        askpass.assert_called_once()
        interactive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
