#!/usr/bin/env python3
"""Tests for multi-session prefill/decode lifecycle orchestration."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "vllm-ascend-pd-serving"


def load_module():
    name = "_pd_serving_test"
    spec = importlib.util.spec_from_file_location(
        name, SKILL / "scripts" / "pd_serving.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pd = load_module()
NOW = "2026-07-25T12:00:00Z"


def group() -> dict:
    snapshot = {"workspace_head": "abc", "submodules": [], "dirty": False}
    return {
        "schema_version": 1,
        "group_id": "pd-group",
        "status": "ready",
        "members": [
            {
                "name": "prefill-member",
                "session_id": "prefill-session",
                "snapshot": snapshot,
            },
            {
                "name": "decode-member",
                "session_id": "decode-session",
                "snapshot": snapshot,
            },
        ],
    }


def config() -> dict:
    return {
        "schema_version": 1,
        "run_id": "pd-run-1",
        "group_id": "pd-group",
        "connector": {"type": "mooncake", "options": {"port": 5000}},
        "services": [
            {
                "name": "decode",
                "member": "decode-member",
                "role": "decode",
                "model": "/models/example",
                "tp": 1,
                "args": ["--kv-transfer-config", '{"kv_role":"kv_consumer"}'],
            },
            {
                "name": "prefill",
                "member": "prefill-member",
                "role": "prefill",
                "model": "/models/example",
                "tp": 1,
                "args": ["--kv-transfer-config", '{"kv_role":"kv_producer"}'],
            },
        ],
        "startup_order": ["decode", "prefill"],
        "proxy": {"base_url": "http://proxy:9000", "health_path": "/health"},
        "smoke": {
            "path": "/v1/chat/completions",
            "request": {
                "model": "example",
                "messages": [{"role": "user", "content": "hello"}],
            },
        },
    }


class PdServingTests(unittest.TestCase):
    def test_rejects_group_member_without_snapshot(self) -> None:
        invalid_group = group()
        invalid_group["members"][0].pop("snapshot")

        with self.assertRaisesRegex(
            pd.PdServingError,
            "snapshot must be a non-empty object",
        ):
            pd.validate_config(config(), invalid_group)

    def test_rejects_mixed_group_snapshots(self) -> None:
        invalid_group = group()
        invalid_group["members"][1]["snapshot"] = {
            "workspace_head": "different",
            "submodules": [],
            "dirty": False,
        }

        with self.assertRaisesRegex(
            pd.PdServingError,
            "share one code snapshot",
        ):
            pd.validate_config(config(), invalid_group)

    def test_rejects_members_that_alias_one_session(self) -> None:
        invalid_group = group()
        invalid_group["members"][1]["session_id"] = invalid_group["members"][0][
            "session_id"
        ]

        with self.assertRaisesRegex(pd.PdServingError, "session_id is duplicated"):
            pd.validate_config(config(), invalid_group)

    def test_requires_both_roles(self) -> None:
        invalid = config()
        invalid["services"][1]["role"] = "decode"
        with self.assertRaisesRegex(pd.PdServingError, "both prefill and decode"):
            pd.validate_config(invalid, group())

    def test_plan_preserves_declared_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            group_path = root / "group.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            group_path.write_text(json.dumps(group()), encoding="utf-8")
            output = root / "run"
            result = pd.plan(
                output,
                config_path=config_path,
                group_path=group_path,
                created_at=NOW,
            )
            self.assertEqual(result["startup_order"], ["decode", "prefill"])
            lifecycle = json.loads(
                (output / "lifecycle.json").read_text(encoding="utf-8")
            )
            self.assertEqual(lifecycle["shutdown"], ["prefill", "decode"])

    def test_partial_start_rolls_back_in_reverse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            group_path = root / "group.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            group_path.write_text(json.dumps(group()), encoding="utf-8")
            output = root / "run"
            pd.plan(
                output,
                config_path=config_path,
                group_path=group_path,
                created_at=NOW,
            )
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                if "serve_start.py" in command[1] and "--session-id" in command:
                    session_id = command[command.index("--session-id") + 1]
                    if session_id == "prefill-session":
                        return subprocess.CompletedProcess(
                            command, 1, '{"status":"failed"}', ""
                        )
                    return subprocess.CompletedProcess(
                        command, 0, '{"status":"ready"}', ""
                    )
                return subprocess.CompletedProcess(
                    command, 0, '{"status":"stopped"}', ""
                )

            result = pd.start(output, runner=runner, updated_at=NOW)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["rollback"], ["decode"])
            self.assertIn("serve_stop.py", calls[-1][1])

    def test_service_command_forwards_role_args(self) -> None:
        service = config()["services"][0]
        member = group()["members"][1]
        command = pd.service_command(service, member, action="start")
        self.assertIn("--kv-transfer-config", command)
        self.assertEqual(
            command[command.index("--session-id") + 1], "decode-session"
        )


if __name__ == "__main__":
    unittest.main()
