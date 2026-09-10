#!/usr/bin/env python3
"""Tests for PD full-group topology admission."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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
CODE = {"source_head": "a" * 40, "snapshot_commit": "b" * 40, "dirty": False}


def config() -> dict:
    return {
        "schema_version": 1,
        "run_id": "pd-run-1",
        "group_id": "pd-group",
        "connector": {"type": "mooncake", "options": {"port": 5000}},
        "services": [
            {
                "name": "decode",
                "role": "decode",
                "model": "/models/example",
                "tp": 1,
                "args": ["--kv-transfer-config", '{"kv_role":"kv_consumer"}'],
            },
            {
                "name": "prefill",
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
    def test_config_is_self_contained(self) -> None:
        pd.validate_config(config())

    def test_rejects_duplicate_service_names(self) -> None:
        invalid = config()
        invalid["services"][1]["name"] = invalid["services"][0]["name"]
        with self.assertRaisesRegex(pd.PdServingError, "service name is duplicated"):
            pd.validate_config(invalid)

    def test_requires_both_roles(self) -> None:
        invalid = config()
        invalid["services"][1]["role"] = "decode"
        with self.assertRaisesRegex(pd.PdServingError, "both prefill and decode"):
            pd.validate_config(invalid)

    def test_topology_contains_every_role_command(self) -> None:
        topology = pd.topology_from_config(config())
        names = [role["name"] for role in topology["roles"]]
        self.assertEqual(names, ["decode", "prefill"])
        for role in topology["roles"]:
            self.assertIn('"$VAWS_PYTHON"', role["command"])
            self.assertIn("--kv-transfer-config", role["command"])
            self.assertEqual(role["npu_count"], 1)

    def test_role_env_is_topology_data_not_shell_json(self) -> None:
        cfg = config()
        cfg["services"][0]["env"] = {"HCCL_BUFFSIZE": "1024$"}
        topology = pd.topology_from_config(cfg)
        decode = next(role for role in topology["roles"] if role["name"] == "decode")
        self.assertEqual(decode["env"]["HCCL_BUFFSIZE"], "1024$")
        self.assertNotIn("export HCCL_BUFFSIZE", decode["command"])
        self.assertNotIn('"1024$"', decode["command"])

    def test_plan_needs_only_business_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "run"
            result = pd.plan(
                output,
                config_path=config_path,
                created_at=NOW,
                code=CODE,
            )
            self.assertEqual(result["startup_order"], ["decode", "prefill"])
            self.assertFalse((output / "session-group.json").exists())
            lifecycle = json.loads((output / "lifecycle.json").read_text(encoding="utf-8"))
            self.assertEqual(len(lifecycle["topology"]["roles"]), 2)

    def test_start_submits_one_topology_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "run"
            pd.plan(
                output,
                config_path=config_path,
                created_at=NOW,
                code=CODE,
            )
            captured: dict = {}

            def fake_run(command, **kwargs):
                captured["command"] = command
                captured.update(kwargs)
                return {
                    "execution_id": "exec-1",
                    "state": "queued",
                    "service": "pd-group",
                }

            client = SimpleNamespace(
                context={"session": {"id": "task-1"}},
                run=fake_run,
            )
            result = pd.start(output, client=client, updated_at=NOW)
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["execution_id"], "exec-1")
            self.assertIn("topology", captured)
            self.assertEqual(len(captured["topology"]["roles"]), 2)
            self.assertEqual(captured["service"], "pd-group")
            self.assertIsNone(captured["timeout_seconds"])
            self.assertNotIn("npu_count", captured)

    def test_preparing_is_queued_with_the_same_execution(self) -> None:
        self.assertIn("preparing", pd.PENDING)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "run"
            pd.plan(
                output,
                config_path=config_path,
                created_at=NOW,
                code=CODE,
            )
            client = SimpleNamespace(
                context={"session": {"id": "task-1"}},
                run=lambda command, **kwargs: {
                    "execution_id": "exec-prep",
                    "state": "preparing",
                    "service": "pd-group",
                },
                observe=lambda eid, action="status", force=False, role=None: {
                    "state": "preparing",
                    "execution_id": eid,
                    "roles": [
                        {"name": "decode", "state": "preparing"},
                        {"name": "prefill", "state": "preparing"},
                    ],
                },
            )
            started = pd.start(output, client=client, updated_at=NOW)
            self.assertEqual(started["status"], "queued")
            self.assertEqual(started["execution_id"], "exec-prep")
            self.assertEqual(started["state"], "preparing")
            self.assertFalse(started["running"])
            result = pd.status(output, client=client)
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["execution_id"], "exec-prep")
            self.assertEqual(result["state"], "preparing")
            self.assertFalse(result["running"])

    def test_stop_uses_the_same_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "run"
            pd.plan(
                output,
                config_path=config_path,
                created_at=NOW,
                code=CODE,
            )
            client = SimpleNamespace(
                context={"session": {"id": "task-1"}},
                run=lambda *a, **k: {"execution_id": "exec-1", "state": "running", "service": "pd-group"},
                observe=lambda eid, action="status", force=False: {"state": "cancelled", "execution_id": eid},
            )
            pd.start(output, client=client, updated_at=NOW)
            result = pd.stop(output, force=True, client=client, updated_at=NOW)
            self.assertEqual(result["execution_id"], "exec-1")
            self.assertEqual(result["status"], "stopped")
            self.assertTrue(result["container_preserved"])


if __name__ == "__main__":
    unittest.main()
