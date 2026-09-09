#!/usr/bin/env python3
"""Tests for grouping task-scoped service names."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
SKILL = ROOT / ".agents" / "skills" / "session-management"
PD = ROOT / ".agents" / "skills" / "vllm-ascend-pd-serving"


def load_module():
    name = "_session_group_test"
    spec = importlib.util.spec_from_file_location(
        name, SKILL / "scripts" / "session_group.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


groups = load_module()


class SessionGroupTests(unittest.TestCase):
    def test_create_output_is_accepted_by_pd_plan(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "_pd_from_group", PD / "scripts" / "pd_serving.py"
        )
        pd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pd)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            groups.WORKSPACE_ROOT = repo
            with mock.patch.object(groups, "resolve_context_file", return_value="/tmp/ctx.json"):
                rc = groups.cmd_create(mock.Mock(
                    group_id="pd-group",
                    member=["prefill=prefill", "decode=decode"],
                    startup_order="",
                    context_file="/tmp/ctx.json",
                ))
            self.assertEqual(rc, 0)
            payload = json.loads((repo / ".vaws-local" / "task-groups" / "pd-group" / "group.json").read_text())
            config = {
                "schema_version": 1,
                "run_id": "pd-run-1",
                "group_id": "pd-group",
                "connector": {"type": "mooncake", "options": {}},
                "services": [
                    {"name": "prefill", "member": "prefill", "role": "prefill", "model": "/m", "tp": 1, "args": []},
                    {"name": "decode", "member": "decode", "role": "decode", "model": "/m", "tp": 1, "args": []},
                ],
                "startup_order": ["prefill", "decode"],
                "proxy": {"base_url": "http://proxy:9000"},
                "smoke": {"path": "/v1/chat/completions", "request": {}},
            }
            pd.validate_config(config, payload)

    def test_rejects_duplicate_services(self) -> None:
        from vaws_session_state import SessionStateError

        with tempfile.TemporaryDirectory() as tmp:
            groups.WORKSPACE_ROOT = Path(tmp)
            with mock.patch.object(groups, "resolve_context_file", return_value="/tmp/ctx.json"):
                with self.assertRaises(SessionStateError):
                    groups.cmd_create(mock.Mock(
                        group_id="pd-group",
                        member=["prefill=vllm", "decode=vllm"],
                        startup_order="",
                        context_file="/tmp/ctx.json",
                    ))


if __name__ == "__main__":
    unittest.main()
