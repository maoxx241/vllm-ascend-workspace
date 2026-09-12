#!/usr/bin/env python3
"""Tests for PD full-group topology admission."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock
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
            self.assertIn("exec \"$VAWS_PYTHON\" -m vllm.entrypoints.cli.main serve", role["command"])
            self.assertIn("--kv-transfer-config", role["command"])
            self.assertEqual(role["npu_count"], 1)
            self.assertNotIn("preflight", role)

    def test_role_env_is_topology_data_not_shell_json(self) -> None:
        cfg = config()
        cfg["services"][0]["env"] = {"HCCL_BUFFSIZE": "1024$"}
        topology = pd.topology_from_config(cfg)
        decode = next(role for role in topology["roles"] if role["name"] == "decode")
        self.assertEqual(decode["env"]["HCCL_BUFFSIZE"], "1024$")
        self.assertNotIn("export HCCL_BUFFSIZE", decode["command"])
        self.assertNotIn('"1024$"', decode["command"])

    def test_start_submits_topology_without_management_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            document = config()
            for field in ("run_id", "schema_version", "startup_order"):
                document.pop(field)
            cfg.write_text(json.dumps(document), encoding="utf-8")
            owner = mock.Mock()
            owner.run.return_value = {"execution_id": "exec-1", "state": "preparing", "service": "pd-group"}
            result = pd.start(cfg, client=owner)
            self.assertEqual(result["state"], "preparing")
            self.assertEqual(result["execution_id"], "exec-1")
            self.assertEqual(len(owner.run.call_args.kwargs["topology"]["roles"]), 2)
            self.assertIsNone(owner.run.call_args.kwargs["timeout_seconds"])
            self.assertEqual(list(Path(tmp).iterdir()), [cfg])

    def test_status_and_stop_need_only_owner_reference(self):
        owner = mock.Mock()
        owner.observe.return_value = {"execution_id": "exec-1", "state": "stopping", "resources_released": False}
        pending = pd.stop(service="pd-group", client=owner, force=True)
        self.assertEqual(pending["state"], "stopping")
        self.assertFalse(pending["resources_released"])
        owner.observe.assert_called_once_with(None, "stop", True, service="pd-group")
        owner.observe.return_value = {"execution_id": "exec-1", "state": "cancelled", "resources_released": True}
        result = pd.status(execution_id="exec-1", client=owner)
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["resources_released"])
        owner.run.assert_not_called()

    def test_health_preserves_lifecycle_and_does_not_probe_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps(config()), encoding="utf-8")
            owner = mock.Mock()
            owner.observe.return_value = {"execution_id": "exec-1", "state": "running"}
            opener = mock.Mock(side_effect=urllib.error.HTTPError("http://proxy:9000/health", 502, "bad gateway", {}, None))
            result = pd.status(service="pd-group", config_path=cfg, client=owner, urlopen=opener)
            self.assertEqual(result["state"], "running")
            self.assertEqual(result["readiness"], "unhealthy")
            self.assertEqual(result["proxy"]["failure"], {"kind": "http_status", "status_code": 502})
            owner.observe.return_value = {"execution_id": "exec-1", "state": "cancelled"}
            opener.reset_mock()
            result = pd.status(service="pd-group", config_path=cfg, client=owner, urlopen=opener)
            opener.assert_not_called()
            self.assertNotIn("readiness", result)

    def test_smoke_records_only_observed_http_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps(config()), encoding="utf-8")
            response = mock.MagicMock()
            response.__enter__.return_value.status = 200
            response.__enter__.return_value.read.return_value = b'{"choices":[{"text":"ok"}]}'
            result = pd.smoke(cfg, urlopen=mock.Mock(return_value=response), output_dir=Path(tmp)/"report")
            self.assertEqual(result["status"], "passed")
            self.assertIn("inspect service logs", result["claim"])
            self.assertEqual(json.loads((Path(tmp)/"report/smoke.json").read_text())["status"], "passed")


if __name__ == "__main__":
    unittest.main()
