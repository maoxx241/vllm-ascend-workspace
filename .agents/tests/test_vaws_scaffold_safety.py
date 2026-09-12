#!/usr/bin/env python3
"""Local safety regression tests for VAWS workspace helpers."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_validate import (  # noqa: E402
    ValidationError,
    parse_device_csv,
    require_env_name,
    require_safe_id,
)


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ValidatorTests(unittest.TestCase):
    def test_safe_id_rejects_path_shapes(self) -> None:
        for value in ("../x", "a/b", "/tmp/x", "..", "a b", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    require_safe_id(value, label="job id")

    def test_env_name_is_ascii_shell_identifier(self) -> None:
        self.assertEqual(require_env_name("VLLM_USE_V1"), "VLLM_USE_V1")
        for value in ("A-B", "1ABC", "A;echo", "", "\u53d8\u91cf"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    require_env_name(value)

    def test_device_csv_rejects_invalid_inputs(self) -> None:
        self.assertEqual(parse_device_csv("2,0,1"), [0, 1, 2])
        for value in ("-1", "0,0", "999,,1", "", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    parse_device_csv(value)


class RunStateIsolationTests(unittest.TestCase):
    def test_memory_profiling_run_dirs_are_unique_and_sanitized(self) -> None:
        module = load_script_module(
            "_vaws_mem_common_test",
            ROOT / ".agents" / "skills" / "ascend-memory-profiling" / "scripts" / "_common.py",
        )
        original_state_dir = module.MEMPROF_STATE_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                module.MEMPROF_STATE_DIR = Path(tmp) / "memory"
                first = module.ensure_run_dir(tag="../same tag")
                second = module.ensure_run_dir(tag="../same tag")
                self.assertNotEqual(first, second)
                self.assertEqual(first.parent, module.MEMPROF_STATE_DIR)
                self.assertEqual(second.parent, module.MEMPROF_STATE_DIR)
                self.assertNotIn("..", first.name)
                self.assertNotIn("/", first.name)
        finally:
            module.MEMPROF_STATE_DIR = original_state_dir

    def test_profiling_collection_run_dirs_include_session_and_do_not_collide(self) -> None:
        module = load_script_module(
            "_vaws_profile_collection_common_test",
            ROOT / ".agents" / "skills" / "ascend-profiling-collection" / "scripts" / "_common.py",
        )
        original_state_dir = module.COLLECTION_STATE_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                module.COLLECTION_STATE_DIR = Path(tmp) / "profile-runs"
                first = module.unique_collection_run_dir(tag="../same tag", session_id="sess-a")
                second = module.unique_collection_run_dir(tag="../same tag", session_id="sess-a")
                self.assertNotEqual(first, second)
                self.assertEqual(first.parent, module.COLLECTION_STATE_DIR)
                self.assertEqual(second.parent, module.COLLECTION_STATE_DIR)
                self.assertIn("sess-a", first.name)
                self.assertNotIn("..", first.name)
                self.assertNotIn("/", first.name)
        finally:
            module.COLLECTION_STATE_DIR = original_state_dir

    def test_benchmark_results_are_written_under_task_state(self) -> None:
        module = load_script_module(
            "_vaws_benchmark_common_test",
            ROOT / ".agents" / "skills" / "vllm-ascend-benchmark" / "scripts" / "_benchmark_common.py",
        )
        original_root = module.ROOT
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                module.ROOT = root
                config = module.BenchConfig(task_id="task-a", model="/models/Qwen")
                payload = {"status": "ok"}
                result_path = module.write_local_result(config, payload)
                expected_parent = root / ".vaws-local" / "tasks" / "task-a" / "benchmark" / "runs"
                self.assertEqual(result_path.parent, expected_parent)
                saved = json.loads(result_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["status"], "ok")
                self.assertEqual(saved["result_path"], str(result_path))
                self.assertEqual(saved["run_dir"], str(expected_parent))
        finally:
            module.ROOT = original_root


if __name__ == "__main__":
    unittest.main()
