#!/usr/bin/env python3
"""Tests for session_create host probes (device enumeration)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
for _p in (str(SCRIPTS),):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

from session_create import parse_host_npu_devices  # noqa: E402

# Dual-chip A3 card layout: 8 cards x 2 chips; chip rows carry Phy-IDs 0-15
# which are the device ids vLLM / ASCEND_RT_VISIBLE_DEVICES actually use.
A3_DUAL_CHIP = """\
+---------------------------+---------------+----------------------------------------------------+
| NPU   Name                | Health        | Power(W)    Temp(C)           Hugepages-Usage(page)|
| Chip  Phy-ID              | Bus-Id        | AICore(%)   Memory-Usage(MB)  HBM-Usage(MB)        |
+===========================+===============+====================================================+
| 0     Ascend910           | OK            | 172.6       52                0    / 0             |
| 0     0                   | 0000:9D:00.0  | 0           0    / 0          3151 / 65536         |
+------------------------------------------------------------------------------------------------+
| 0     Ascend910           | OK            | -           54                0    / 0             |
| 1     1                   | 0000:9F:00.0  | 0           0    / 0          2884 / 65536         |
+===========================+===============+====================================================+
| 1     Ascend910           | OK            | 170.9       53                0    / 0             |
| 0     2                   | 0000:99:00.0  | 0           0    / 0          3136 / 65536         |
+------------------------------------------------------------------------------------------------+
| 1     Ascend910           | OK            | -           53                0    / 0             |
| 1     3                   | 0000:9B:00.0  | 0           0    / 0          2895 / 65536         |
+===========================+===============+====================================================+
| NPU     Chip              | Process id    | Process name             | Process memory(MB)      |
+===========================+===============+====================================================+
| No running processes found                                                          |
+------------------------------------------------------------------------------------------------+
"""

# Single-chip layout (910B4-style): one Phy-ID row per card.
SINGLE_CHIP = """\
+---------------------------+---------------+----------------------------------------------------+
| NPU   Name                | Health        | Power(W)    Temp(C)           Hugepages-Usage(page)|
| Chip  Phy-ID              | Bus-Id        | AICore(%)   Memory-Usage(MB)  HBM-Usage(MB)        |
+===========================+===============+====================================================+
| 0     Ascend910B4         | OK            | 72.6        42                0    / 0             |
| 0     0                   | 0000:C1:00.0  | 0           0    / 0          1151 / 32768         |
+------------------------------------------------------------------------------------------------+
| 1     Ascend910B4         | OK            | 70.1        41                0    / 0             |
| 0     1                   | 0000:C2:00.0  | 0           0    / 0          1150 / 32768         |
+===========================+===============+====================================================+
| NPU     Chip              | Process id    | Process name             | Process memory(MB)      |
+===========================+===============+====================================================+
| No running processes found                                                          |
+------------------------------------------------------------------------------------------------+
"""

# Header-only fallback (no chip rows parseable): keep the old behaviour.
HEADER_ONLY = """\
| 0     Ascend910           | OK            | 172.6       52                0    / 0             |
| 1     Ascend910           | OK            | 170.9       53                0    / 0             |
"""


class ParseHostNpuDevicesTests(unittest.TestCase):
    def test_dual_chip_a3_returns_phy_ids(self) -> None:
        self.assertEqual(parse_host_npu_devices(A3_DUAL_CHIP), [0, 1, 2, 3])

    def test_single_chip_returns_phy_ids(self) -> None:
        self.assertEqual(parse_host_npu_devices(SINGLE_CHIP), [0, 1])

    def test_header_only_falls_back_to_card_ids(self) -> None:
        self.assertEqual(parse_host_npu_devices(HEADER_ONLY), [0, 1])

    def test_empty_output(self) -> None:
        self.assertEqual(parse_host_npu_devices(""), [])


if __name__ == "__main__":
    unittest.main()
