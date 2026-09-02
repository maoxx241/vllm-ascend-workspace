#!/usr/bin/env python3
"""Focused regression tests for npu-smi hardware detection."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import manage_machine as machine_ops  # noqa: E402


class NpuSmiDetectionTests(unittest.TestCase):
    def test_a3_combines_generic_chip_name_and_npu_name(self) -> None:
        board = """
        NPU Name                      : 9362
        Chip Name                     : Ascend910
        Chip Version                  : V1
        """
        soc = machine_ops.canonical_soc_from_board_text(board)
        self.assertEqual(soc, "ascend910_9362")
        self.assertEqual(machine_ops.machine_type_from_soc(soc), "A3")

    def test_a2_combines_chip_type_and_chip_name(self) -> None:
        board = """
        Chip Name                     : 910B4
        Chip Type                     : Ascend
        """
        soc = machine_ops.canonical_soc_from_board_text(board)
        self.assertEqual(soc, "ascend910b4")
        self.assertEqual(machine_ops.machine_type_from_soc(soc), "A2")

    def test_310p_combines_chip_type_and_chip_name(self) -> None:
        board = """
        Chip Name                     : 310P3
        Chip Type                     : Ascend
        """
        soc = machine_ops.canonical_soc_from_board_text(board)
        self.assertEqual(soc, "ascend310p3")
        self.assertEqual(machine_ops.machine_type_from_soc(soc), "310P")

    def test_a5_accepts_the_ascend950_soc_family(self) -> None:
        board = """
        NPU Name                      : 1910
        Chip Name                     : Ascend950DT
        """
        soc = machine_ops.canonical_soc_from_board_text(board)
        self.assertEqual(soc, "ascend950dt_1910")
        self.assertEqual(machine_ops.machine_type_from_soc(soc), "A5")
        self.assertEqual(
            machine_ops.detect_machine_type_from_text("SOC_VERSION=Ascend950DT_1910"),
            ("ascend950dt_1910", "A5"),
        )

    def test_bare_ascend910_is_an_a3_compatibility_fallback(self) -> None:
        self.assertEqual(
            machine_ops.detect_machine_type_from_text("Chip Name: Ascend910"),
            ("ascend910", "A3"),
        )

    def test_a5_selector_and_explicit_image_suffixes_are_recognized(self) -> None:
        self.assertEqual(machine_ops.image_tag_for_machine("main", "A5"), "main-a5")
        for tag in ("v0.13.0-a5", "v0.13.0-a5-openeuler", "v0.12.0-950dt"):
            with self.subTest(tag=tag):
                self.assertEqual(
                    machine_ops.infer_machine_type_from_image(
                        f"quay.io/ascend/vllm-ascend:{tag}"
                    ),
                    "A5",
                )

    def test_remote_probe_uses_precise_board_queries_before_env_fallback(self) -> None:
        rendered = machine_ops.render_host_probe_script()
        self.assertIn('["npu-smi", "info", "-m"]', rendered)
        self.assertIn('"board-device"', rendered)
        self.assertIn('"board-chip"', rendered)
        self.assertLess(
            rendered.index('precise_candidates = ['),
            rendered.index('soc_from_env = os.environ.get("SOC_VERSION")'),
        )
        self.assertIn('"persisted-env-fallback"', rendered)


if __name__ == "__main__":
    unittest.main()
