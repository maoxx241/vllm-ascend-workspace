from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from guard_common import inspect_command, inspect_payload  # noqa: E402


class HookGuardTests(unittest.TestCase):
    def test_allows_raw_ssh(self) -> None:
        self.assertFalse(inspect_command("ssh root@1.2.3.4 -p 46000 hostname").blocked)

    def test_allows_password_helpers(self) -> None:
        self.assertFalse(inspect_command("sshpass -p secret ssh host").blocked)

    def test_allows_normal_local_command(self) -> None:
        self.assertFalse(inspect_command("python3 -m compileall .remote-dev").blocked)

    def test_allows_remote_path_apply_patch(self) -> None:
        decision = inspect_payload({"tool_name": "apply_patch", "command": "*** Update File: /vllm-workspace/foo.py"})
        self.assertFalse(decision.blocked)

    def test_allows_remote_mcp_path_escape(self) -> None:
        decision = inspect_payload(
            {
                "tool_name": "mcp__remote-dev__remote_read",
                "tool_input": {
                    "host": "1.2.3.4",
                    "port": 46000,
                    "root": "/vllm-workspace",
                    "file_path": "/etc/passwd",
                },
            }
        )
        self.assertFalse(decision.blocked)

    def test_allows_remote_mcp_cwd_escape(self) -> None:
        decision = inspect_payload(
            {
                "tool_name": "remote.bash",
                "arguments": {
                    "host": "1.2.3.4",
                    "port": 46000,
                    "root": "/vllm-workspace",
                    "cwd": "/tmp",
                    "command": "pwd",
                },
            }
        )
        self.assertFalse(decision.blocked)

    def test_allows_remote_mcp_secret_in_command(self) -> None:
        decision = inspect_payload(
            {
                "tool_name": "remote.bash",
                "arguments": {
                    "host": "1.2.3.4",
                    "port": 46000,
                    "command": "curl --password secret",
                },
            }
        )
        self.assertFalse(decision.blocked)

    def test_allows_remote_mcp_path_under_root(self) -> None:
        decision = inspect_payload(
            {
                "tool_name": "mcp__remote-dev__remote_read",
                "tool_input": {
                    "host": "1.2.3.4",
                    "port": 46000,
                    "root": "/vllm-workspace",
                    "cwd": "/vllm-workspace/vllm-ascend",
                    "file_path": "README.md",
                },
            }
        )
        self.assertFalse(decision.blocked)

    def test_claude_hook_allows_raw_ssh(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "ssh root@1.2.3.4 hostname"}}
        proc = subprocess.run(
            [sys.executable, str(HOOKS / "claude_remote_guard.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")

    def test_claude_hook_allows_mcp_remote_bash_secret_like_command(self) -> None:
        payload = {
            "tool_name": "mcp__remote-dev__remote_bash",
            "tool_input": {"command": "echo token=abc", "host": "1.2.3.4", "port": 46000},
        }
        proc = subprocess.run(
            [sys.executable, str(HOOKS / "claude_remote_guard.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")

    def test_claude_settings_hooks_mcp_remote_tools(self) -> None:
        settings = json.loads((REPO_ROOT / ".claude" / "settings.example.json").read_text(encoding="utf-8"))
        matchers = {item["matcher"] for item in settings["hooks"]["PreToolUse"]}
        self.assertIn("mcp__remote-dev__.*", matchers)

    def test_codex_hook_returns_allow_json_shape(self) -> None:
        payload = {"tool_name": "remote.bash", "arguments": {"command": "curl --password secret"}}
        proc = subprocess.run(
            [sys.executable, str(HOOKS / "codex_remote_guard.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(data["decision"], "allow")


if __name__ == "__main__":
    unittest.main()
