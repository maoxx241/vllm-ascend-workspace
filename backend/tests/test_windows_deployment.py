from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


class WindowsDeploymentTests(unittest.TestCase):
    def test_supervisor_resolves_windows_npm_cmd(self) -> None:
        path = ROOT / "scripts" / "supervisor.py"
        spec = importlib.util.spec_from_file_location("nfm_supervisor", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        env = {"NFM_BIND": "127.0.0.1", "NFM_WEB_PORT": "8788"}
        with mock.patch.object(module.shutil, "which", side_effect=lambda name: "C:\\Node\\npm.cmd" if name == "npm.cmd" else None):
            command = module.frontend_command(env, platform="nt")
        self.assertEqual(command[0], "C:\\Node\\npm.cmd")
        self.assertEqual(command[-4:], ["--", "--hostname", "127.0.0.1", "--port", "8788"][-4:])

    def test_windows_installer_is_loopback_only_and_managed(self) -> None:
        installer = (ROOT / "scripts" / "install-windows-service.ps1").read_text(encoding="utf-8")
        manager = (ROOT / "scripts" / "manage-windows-service.ps1").read_text(encoding="utf-8")
        common = (ROOT / "scripts" / "windows-common.ps1").read_text(encoding="utf-8")
        self.assertIn("Register-ScheduledTask", installer)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", installer)
        self.assertIn("RestartCount 10", installer)
        self.assertIn("NFM_BIND = '127.0.0.1'", common)
        self.assertNotIn("0.0.0.0", installer + common)
        for action in ("start", "stop", "restart", "status", "logs", "uninstall"):
            self.assertIn(f"'{action}'", manager)
        self.assertIn("History, SSH keys, and configuration were preserved", manager)


if __name__ == "__main__":
    unittest.main()
