"""Measured units, physical device IDs and attribution boundaries."""
from __future__ import annotations

import importlib.util
import io
import json
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


analyze = load("memory_analysis_under_test", "mem_analyze.py")
weights = load("memory_weights_under_test", "weight_inspector.py")


class MemoryEvidenceTests(unittest.TestCase):
    def test_weight_log_units_are_normalized(self):
        gib = analyze.parse_vllm_logs("Loading model weights took 1 GiB")
        gb = analyze.parse_vllm_logs("Loading model weights took 1.073741824 GB")
        self.assertEqual(gib["weights_gib"], 1)
        self.assertEqual(gb["weights_gib"], 1)

    def test_physical_devices_and_unassigned_process_data_stay_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csvs = root / "msprof_csvs"
            csvs.mkdir()
            for name, value in (("npu_module_mem_global.csv", 20), ("npu_module_mem_one.csv", 5)):
                (csvs / name).write_text("Component,Total Reserved(MB)\nHCCL," + str(value) + "\n", encoding="utf-8")
            manifest = {"model": "fixture", "tp": 2, "dp": 1, "devices": "3,4", "baseline_source": "unavailable",
                "after_ready_hbm": {str(i): {"used_mb": 10000, "total_mb": 64000} for i in (0, 3, 4)},
                "after_infer_hbm": {"3": {"used_mb": 10500}},
                "msprof_csvs": {"__prof_device_map__": {"npu_module_mem_global.csv": [3, 4], "npu_module_mem_one.csv": [3]}}}
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (root / "vllm_serve.log").write_text("Loading model weights took 1.073741824 GB\n", encoding="utf-8")
            with mock.patch.object(sys, "argv", ["mem_analyze.py", str(root)]), redirect_stdout(io.StringIO()):
                analyze.main()
            result = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual([item["device_id"] for item in result["devices"]], [3, 4])
            self.assertEqual(result["msprof_per_device"], {"3": {"HCCL": 5.0}})
            self.assertEqual(result["msprof_components_global"]["HCCL"], 20)
            first = result["devices"][0]
            self.assertEqual(first["cross_validation"]["post_inference_delta_mb"], 500)
            self.assertEqual(first["cross_validation"]["component_sum_mb"], 1029)
            self.assertEqual(result["devices"][1]["cross_validation"]["component_sum_mb"], 1024)

    def test_truncated_or_out_of_range_safetensors_is_not_zero_byte_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weights.safetensors"
            path.write_bytes(b"short")
            with self.assertRaisesRegex(ValueError, "truncated"):
                weights.read_safetensors_header(str(path))
            header = json.dumps({"weight": {"dtype": "F16", "shape": [2], "data_offsets": [0, 4]}}).encode()
            path.write_bytes(struct.pack("<Q", len(header)) + header + b"xx")
            with self.assertRaisesRegex(ValueError, "byte range"):
                weights.read_safetensors_header(str(path))
