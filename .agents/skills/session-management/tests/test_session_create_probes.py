#!/usr/bin/env python3
"""Tests for session_create host probes (device enumeration)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
LIB = ROOT / ".agents" / "lib"
for _p in (str(LIB), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import session_create  # noqa: E402
from session_create import (  # noqa: E402
    parse_host_npu_availability,
    parse_host_npu_devices,
    probe_host_npu_devices,
)
from vaws_session_state import (  # noqa: E402
    SessionStateError,
    allocate_session_leases,
    session_leases_path,
    session_live_leases,
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

# visible [0,1], busy [0], free [1]
SINGLE_CHIP_BUSY_0 = SINGLE_CHIP.replace(
    "| No running processes found                                                          |",
    "| 0       0                 | 3412          | python                   | 41235                   |",
)

# visible [0], busy [0], free [], status ok
SINGLE_DEVICE_ALL_BUSY = """\
+---------------------------+---------------+----------------------------------------------------+
| NPU   Name                | Health        | Power(W)    Temp(C)           Hugepages-Usage(page)|
| Chip  Phy-ID              | Bus-Id        | AICore(%)   Memory-Usage(MB)  HBM-Usage(MB)        |
+===========================+===============+====================================================+
| 0     Ascend910B4         | OK            | 72.6        42                0    / 0             |
| 0     0                   | 0000:C1:00.0  | 0           0    / 0          1151 / 32768         |
+===========================+===============+====================================================+
| NPU     Chip              | Process id    | Process name             | Process memory(MB)      |
+===========================+===============+====================================================+
| 0       0                 | 3412          | python                   | 41235                   |
+------------------------------------------------------------------------------------------------+
"""

HOST_RECORD = {"host": {"ip": "192.0.2.10", "user": "root", "port": 22}}
MACHINE = "host"


class ParseHostNpuDevicesTests(unittest.TestCase):
    def test_dual_chip_a3_returns_phy_ids(self) -> None:
        self.assertEqual(parse_host_npu_devices(A3_DUAL_CHIP), [0, 1, 2, 3])

    def test_single_chip_returns_phy_ids(self) -> None:
        self.assertEqual(parse_host_npu_devices(SINGLE_CHIP), [0, 1])

    def test_header_only_falls_back_to_card_ids(self) -> None:
        self.assertEqual(parse_host_npu_devices(HEADER_ONLY), [0, 1])

    def test_empty_output(self) -> None:
        self.assertEqual(parse_host_npu_devices(""), [])


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


def _probe_host(*, stdout: str = "", returncode: int = 0, timeout: bool = False):
    if timeout:
        runner = mock.Mock(
            side_effect=subprocess.TimeoutExpired(cmd=["ssh"], timeout=30, output="", stderr="")
        )
    else:
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(
                args=["ssh"],
                returncode=returncode,
                stdout=stdout,
                stderr="" if returncode == 0 else "ssh failed",
            )
        )
    with mock.patch.object(session_create.subprocess, "run", runner):
        return probe_host_npu_devices(HOST_RECORD)


def _allocate(root: Path, session_id: str, **kwargs):
    return allocate_session_leases(
        repo_root=root,
        machine_alias=MACHINE,
        session_id=session_id,
        port_available=lambda _port: True,
        **kwargs,
    )


def _live(root: Path, session_id: str) -> dict:
    return session_live_leases(repo_root=root, machine_alias=MACHINE, session_id=session_id)


class ProbeHostNpuDevicesAllocationTests(unittest.TestCase):
    """Pin the probe wrapper, not just the parser, then feed the allocator."""

    def _assert_no_lease_or_port(self, root: Path, session_id: str) -> None:
        self.assertFalse(session_leases_path(root).exists())
        self.assertEqual(
            _live(root, session_id),
            {"npu_devices": [], "container_ssh_ports": [], "service_ports": []},
        )

    def test_partial_busy_allocates_only_the_free_device(self) -> None:
        free, payload = _probe_host(stdout=SINGLE_CHIP_BUSY_0)
        self.assertEqual(free, [1])
        self.assertEqual(payload["availability"], "known")
        self.assertEqual(payload["visible_devices"], [0, 1])
        self.assertEqual(payload["busy_devices"], [0])
        self.assertEqual(payload["free_devices"], [1])
        self.assertEqual(payload["status"], "ok")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explicit = _allocate(
                root, "sess-explicit", requested_devices=[1], available_devices=free, container_ssh_port=46001
            )
            self.assertEqual(explicit["npu_devices"], [1])
            self.assertEqual(_live(root, "sess-explicit")["npu_devices"], [1])
            self.assertEqual(_live(root, "sess-explicit")["container_ssh_ports"], [46001])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counted = _allocate(root, "sess-count", npu_count=1, available_devices=free, container_ssh_port=46002)
            self.assertEqual(counted["npu_devices"], [1])
            self.assertEqual(_live(root, "sess-count")["npu_devices"], [1])
            self.assertEqual(_live(root, "sess-count")["container_ssh_ports"], [46002])

    def test_partial_busy_refuses_busy_device_and_oversize_count(self) -> None:
        free, _payload = _probe_host(stdout=SINGLE_CHIP_BUSY_0)
        self.assertEqual(free, [1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SessionStateError):
                _allocate(root, "sess-busy", requested_devices=[0], available_devices=free)
            self._assert_no_lease_or_port(root, "sess-busy")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SessionStateError):
                _allocate(root, "sess-count", npu_count=2, available_devices=free)
            self._assert_no_lease_or_port(root, "sess-count")

    def test_known_empty_free_set_returns_empty_list_and_refuses_both_modes(self) -> None:
        free, payload = _probe_host(stdout=SINGLE_DEVICE_ALL_BUSY)
        self.assertEqual(free, [])
        self.assertIsNotNone(free)
        self.assertEqual(payload["availability"], "known")
        self.assertEqual(payload["visible_devices"], [0])
        self.assertEqual(payload["busy_devices"], [0])
        self.assertEqual(payload["free_devices"], [])
        self.assertEqual(payload["status"], "ok")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SessionStateError):
                _allocate(root, "sess-explicit", requested_devices=[0], available_devices=free)
            with self.assertRaises(SessionStateError):
                _allocate(root, "sess-count", npu_count=1, available_devices=free)
            self._assert_no_lease_or_port(root, "sess-explicit")
            self._assert_no_lease_or_port(root, "sess-count")

    def test_unreadable_process_table_returns_none_and_refuses_both_modes(self) -> None:
        free, payload = _probe_host(stdout=HEADER_ONLY)
        self.assertIsNone(free)
        self.assertEqual(payload["availability"], "unknown")
        self.assertEqual(payload["status"], "occupancy_unknown")
        self.assertTrue(payload["availability_reason"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(SessionStateError, "occupancy is unavailable"):
                _allocate(root, "sess-explicit", requested_devices=[0], available_devices=free)
            with self.assertRaisesRegex(SessionStateError, "occupancy is unavailable"):
                _allocate(root, "sess-count", npu_count=1, available_devices=free)
            self._assert_no_lease_or_port(root, "sess-explicit")
            self._assert_no_lease_or_port(root, "sess-count")

    def test_ssh_failure_returns_none_and_does_not_fall_back_to_visibility(self) -> None:
        free, payload = _probe_host(stdout=SINGLE_CHIP, returncode=255)
        self.assertIsNone(free)
        self.assertEqual(payload["status"], "unavailable")
        self.assertNotIn("availability", payload)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(SessionStateError, "occupancy is unavailable"):
                _allocate(root, "sess-explicit", requested_devices=[0], available_devices=free)
            with self.assertRaisesRegex(SessionStateError, "occupancy is unavailable"):
                _allocate(root, "sess-count", npu_count=1, available_devices=free)
            self._assert_no_lease_or_port(root, "sess-explicit")
            self._assert_no_lease_or_port(root, "sess-count")

    def test_probe_timeout_returns_none_and_refuses_both_modes(self) -> None:
        free, payload = _probe_host(timeout=True)
        self.assertIsNone(free)
        self.assertEqual(payload["status"], "timeout")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(SessionStateError, "occupancy is unavailable"):
                _allocate(root, "sess-explicit", requested_devices=[0], available_devices=free)
            with self.assertRaisesRegex(SessionStateError, "occupancy is unavailable"):
                _allocate(root, "sess-count", npu_count=1, available_devices=free)
            self._assert_no_lease_or_port(root, "sess-explicit")
            self._assert_no_lease_or_port(root, "sess-count")

    def test_locally_leased_free_device_is_excluded_by_existing_ownership(self) -> None:
        free, _payload = _probe_host(stdout=SINGLE_CHIP_BUSY_0)
        self.assertEqual(free, [1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owner = _allocate(
                root, "sess-owner", requested_devices=[1], available_devices=free, container_ssh_port=46001
            )
            self.assertEqual(owner["npu_devices"], [1])
            with self.assertRaisesRegex(SessionStateError, "already leased"):
                _allocate(root, "sess-other", requested_devices=[1], available_devices=free)
            with self.assertRaisesRegex(SessionStateError, "not enough allocatable"):
                _allocate(root, "sess-count", npu_count=1, available_devices=free)
            self.assertEqual(_live(root, "sess-owner")["npu_devices"], [1])
            self.assertEqual(_live(root, "sess-owner")["container_ssh_ports"], [46001])
            self.assertEqual(
                _live(root, "sess-other"),
                {"npu_devices": [], "container_ssh_ports": [], "service_ports": []},
            )
            self.assertEqual(
                _live(root, "sess-count"),
                {"npu_devices": [], "container_ssh_ports": [], "service_ports": []},
            )

    def test_no_npu_request_still_allocates_a_port_when_occupancy_is_unknown(self) -> None:
        free, payload = _probe_host(stdout=HEADER_ONLY)
        self.assertIsNone(free)
        self.assertEqual(payload["availability"], "unknown")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            leases = _allocate(root, "sess-port", available_devices=free, container_ssh_port=46009)
            self.assertEqual(leases["npu_devices"], [])
            self.assertEqual(leases["container_ssh_port"], 46009)
            self.assertEqual(_live(root, "sess-port")["npu_devices"], [])
            self.assertEqual(_live(root, "sess-port")["container_ssh_ports"], [46009])


if __name__ == "__main__":
    unittest.main()
