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

from session_create import (  # noqa: E402
    parse_host_npu_availability,
    parse_host_npu_devices,
)

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

# Same A3 layout, but somebody else is already running on Phy-ID 2. This is the
# case that used to be invisible to allocation: the device is healthy, visible
# and taken.
A3_WITH_RUNNING_PROCESS = A3_DUAL_CHIP.replace(
    "| No running processes found                                                          |",
    "| 1       0                 | 3412          | python                   | 41235                   |",
)


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


class ParseHostNpuAvailabilityTests(unittest.TestCase):
    """Allocation must never be driven by the visible-device list.

    Two ways that was wrong, both of which handed out cards somebody else was
    using, and neither of which raised anything:

    * a visible device can be busy, and the parser's occupancy was discarded
    * a parser that fails closed still reports every device with an empty busy
      table, which reads as "all free" to a caller that only looks at devices
    """

    def test_idle_host_offers_every_visible_device(self) -> None:
        free, diagnostics = parse_host_npu_availability(A3_DUAL_CHIP)
        self.assertEqual([0, 1, 2, 3], free)
        self.assertEqual("known", diagnostics["availability"])
        self.assertEqual([], diagnostics["busy_devices"])

    def test_a_device_in_use_is_not_offered(self) -> None:
        free, diagnostics = parse_host_npu_availability(A3_WITH_RUNNING_PROCESS)
        self.assertIsNotNone(free)
        self.assertNotIn(2, free, "a device with a running process must not be allocatable")
        self.assertIn(2, diagnostics["busy_devices"])
        self.assertEqual([0, 1, 2, 3], diagnostics["visible_devices"])

    def test_unreadable_process_table_yields_unknown_not_empty(self) -> None:
        # HEADER_ONLY has no process table at all, so occupancy cannot be
        # established. The old code returned [0, 1] here and allocation
        # proceeded; assign_resources' fail-fast guard for unknown availability
        # was unreachable because a failed probe never produced None.
        free, diagnostics = parse_host_npu_availability(HEADER_ONLY)
        self.assertIsNone(free)
        self.assertEqual("unknown", diagnostics["availability"])
        self.assertTrue(diagnostics["availability_reason"])

    def test_visibility_helper_still_reports_visible_devices(self) -> None:
        # parse_host_npu_devices keeps its meaning: what exists, not what is
        # allocatable. Callers that need the latter must ask for availability.
        self.assertEqual([0, 1], parse_host_npu_devices(HEADER_ONLY))
        self.assertIsNone(parse_host_npu_availability(HEADER_ONLY)[0])
