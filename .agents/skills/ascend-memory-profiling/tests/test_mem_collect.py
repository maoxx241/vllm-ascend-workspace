#!/usr/bin/env python3
"""Regression tests for mem_collect standalone port lease and request quoting."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
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
    spec = importlib.util.spec_from_file_location("_mem_collect_test", SCRIPTS / "mem_collect.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mem_collect = load_module()

import vaws_session_state  # noqa: E402  (resolved via LIB on sys.path)

EP = mem_collect.SshEndpoint(host="192.0.2.10", port=46001, user="root")
MACHINE = {"alias": "machine-a", "host": {"ip": "192.0.2.10"}}


def standalone_args(tmp: str) -> argparse.Namespace:
    return argparse.Namespace(
        model="/models/Qwen",
        tp=None,
        dp=None,
        port=None,
        devices="0",
        tag=None,
        session_id="sess-a",
        session_file=None,
        health_timeout=60,
        gpu_memory_utilization=0.9,
        max_model_len=4096,
        speculative_config=None,
        compilation_config=None,
        additional_config=None,
        quantization=None,
        enforce_eager=False,
        image_url=None,
        prompt="hello",
        max_tokens=16,
        _state_repo_root=Path(tmp),
        _session={},
    )


def patch_phases(module, **overrides):
    """Neutralize every remote/collection phase; overrides inject failures."""
    patches = {
        "ssh_exec": mock.DEFAULT,
        "ensure_run_dir": mock.Mock(return_value=Path(tempfile.mkdtemp())),
        "find_python": mock.Mock(return_value="/usr/bin/python3"),
        "check_msprof_available": mock.DEFAULT,
        "collect_npu_smi": mock.Mock(return_value={}),
        "start_service_with_msprof": mock.DEFAULT,
        "wait_for_health": mock.Mock(return_value=1.0),
        "collect_vllm_logs": mock.Mock(return_value=""),
        "stop_service": mock.DEFAULT,
        "send_inference": mock.Mock(return_value={}),
        "run_msprof_export": mock.DEFAULT,
        "collect_msprof_csvs": mock.Mock(return_value=[]),
        "collect_model_config": mock.Mock(return_value={}),
        "collect_weight_manifest": mock.DEFAULT,
    }
    patches.update(overrides)
    return mock.patch.multiple(module, **patches)


class StandalonePortLeaseTests(unittest.TestCase):
    def test_phase_failure_releases_leased_port(self) -> None:
        # A non-TimeoutError failure (e.g. the service start raising) used to
        # skip every release path and leak the leased session port.
        with tempfile.TemporaryDirectory() as tmp:
            args = standalone_args(tmp)
            with (
                mock.patch.object(vaws_session_state, "allocate_service_port", return_value=47001),
                mock.patch.object(vaws_session_state, "release_service_port") as release,
                patch_phases(
                    mem_collect,
                    start_service_with_msprof=mock.Mock(side_effect=RuntimeError("msprof missing")),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "msprof missing"):
                    mem_collect._main_standalone(args, MACHINE, EP)

        release.assert_called_once()
        self.assertEqual(release.call_args.kwargs["port"], 47001)
        self.assertEqual(release.call_args.kwargs["session_id"], "sess-a")

    def test_health_timeout_still_releases_leased_port_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = standalone_args(tmp)
            with (
                mock.patch.object(vaws_session_state, "allocate_service_port", return_value=47002),
                mock.patch.object(vaws_session_state, "release_service_port") as release,
                patch_phases(
                    mem_collect,
                    wait_for_health=mock.Mock(side_effect=TimeoutError("not ready")),
                ),
            ):
                with self.assertRaises(SystemExit):
                    mem_collect._main_standalone(args, MACHINE, EP)

        release.assert_called_once()
        self.assertEqual(release.call_args.kwargs["port"], 47002)

    def test_success_releases_leased_port_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = standalone_args(tmp)
            with (
                mock.patch.object(vaws_session_state, "allocate_service_port", return_value=47003),
                mock.patch.object(vaws_session_state, "release_service_port") as release,
                patch_phases(mem_collect),
            ):
                mem_collect._main_standalone(args, MACHINE, EP)

        release.assert_called_once()
        self.assertEqual(release.call_args.kwargs["port"], 47003)


class SendInferenceQuotingTests(unittest.TestCase):
    def test_json_payload_is_shell_quoted(self) -> None:
        # A prompt containing a single quote must not break out of the curl
        # -d argument when the payload is interpolated into the remote shell.
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
