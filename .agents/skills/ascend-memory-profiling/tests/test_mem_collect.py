#!/usr/bin/env python3
"""Regression tests for mem_collect request quoting."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".agents" / "skills" / "ascend-memory-profiling" / "scripts"
LIB = ROOT / ".agents" / "lib"
for path in (str(SCRIPTS), str(LIB)):
    if path not in sys.path:
        sys.path.insert(0, path)


def load_module():
    sys.modules.pop("_common", None)
    spec = importlib.util.spec_from_file_location("_mem_collect_test", SCRIPTS / "mem_collect.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mem_collect = load_module()
EP = mem_collect.SshEndpoint(host="192.0.2.10", port=46001, user="root")


class SendInferenceQuotingTests(unittest.TestCase):
    def test_json_payload_is_shell_quoted(self) -> None:
        args = argparse.Namespace(image_url=None, prompt="it's a test", max_tokens=16, model="model", port=None)
        captured: list[str] = []

        def fake_ssh_exec(ep, script, **kwargs):
            captured.append(script)
            return subprocess.CompletedProcess([], 0, "{}", "")

        with mock.patch.object(mem_collect, "ssh_exec", side_effect=fake_ssh_exec):
            mem_collect.send_inference(EP, args, port=8000)

        self.assertEqual(len(captured), 1)
        cmd = captured[0]
        import shlex

        expected_payload = json.dumps(
            {"model": "model", "prompt": "it's a test", "max_tokens": 16, "temperature": 0.7}
        )
        self.assertIn(f"-d {shlex.quote(expected_payload)}", cmd)


if __name__ == "__main__":
    unittest.main()
