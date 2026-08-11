#!/usr/bin/env python3
"""Tests for the optional host-shared NPU coordination protocol."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_npu_coordination import (  # noqa: E402
    NpuCoordinator,
    _confirmed_free_probe,
    parse_npu_smi_info,
)


class FakeClock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def occupancy(*, devices: list[int] | None = None, busy: list[int] | None = None) -> dict:
    visible = devices or [0, 1, 2, 3]
    busy_set = set(busy or [])
    return {
        "status": "ok",
        "devices": visible,
        "busy": {str(item): [{"kind": "test"}] for item in sorted(busy_set)},
        "free": [item for item in visible if item not in busy_set],
    }


class CoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.coordinator = NpuCoordinator(self.temp.name, clock=self.clock)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def submit(self, task_id: str, **extra):
        request = {
            "task_id": task_id,
            "agent_id": f"agent-{task_id}",
            "agent_alias": "team42",
            "npu_count": 1,
            "queue_ttl_seconds": 600,
            "estimated_duration_seconds": 60,
            **extra,
        }
        if "devices" in extra:
            request.pop("npu_count", None)
        return self.coordinator.submit(request)

    def test_task_snapshot_carries_uuid_owner_alias(self) -> None:
        result = self.coordinator.submit(
            {
                "task_id": "task-identity",
                "agent_id": "dce488a7-1af2-44b1-bb91-c3984743d33e",
                "agent_alias": "team42",
                "npu_count": 1,
                "queue_ttl_seconds": 600,
                "estimated_duration_seconds": 60,
            }
        )
        self.assertEqual(result["task"]["agent_id"], "dce488a7-1af2-44b1-bb91-c3984743d33e")
        self.assertEqual(result["task"]["agent_alias"], "team42")

    def activate(self, task_id: str, *, heartbeat_ttl: int = 10) -> int:
        granted = self.coordinator.acquire(task_id, occupancy(), grant_ttl_seconds=10)
        token = granted["task"]["fence_token"]
        self.assertEqual(granted["status"], "granted")
        self.assertEqual(
            self.coordinator.preflight(task_id, token, occupancy(), start_ttl_seconds=10)["status"],
            "starting",
        )
        self.assertEqual(
            self.coordinator.activate(
                task_id,
                token,
                pid=1234,
                heartbeat_ttl_seconds=heartbeat_ttl,
            )["status"],
            "active",
        )
        return token

    def test_strict_fifo_and_atomic_multi_device_grants(self) -> None:
        self.submit("task-a", npu_count=2)
        self.submit("task-b", npu_count=2)

        waiting = self.coordinator.acquire("task-b", occupancy())
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(waiting["reason"], "strict_fifo")
        self.assertEqual(waiting["ahead_task_id"], "task-a")

        first = self.coordinator.acquire("task-a", occupancy())
        second = self.coordinator.acquire("task-b", occupancy())
        self.assertEqual(first["task"]["granted_devices"], [0, 1])
        self.assertEqual(second["task"]["granted_devices"], [2, 3])
        self.assertNotEqual(first["task"]["fence_token"], second["task"]["fence_token"])

    def test_concurrent_agents_receive_disjoint_atomic_grants(self) -> None:
        self.submit("task-concurrent-a", npu_count=2)
        self.submit("task-concurrent-b", npu_count=2)
        barrier = threading.Barrier(2)

        def worker(task_id: str) -> list[int]:
            coordinator = NpuCoordinator(self.temp.name, clock=self.clock)
            barrier.wait()
            for _ in range(100):
                result = coordinator.acquire(task_id, occupancy())
                if result["status"] == "granted":
                    return result["task"]["granted_devices"]
                time.sleep(0.001)
            raise AssertionError(f"task did not receive a grant: {task_id}")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker, "task-concurrent-a")
            second = pool.submit(worker, "task-concurrent-b")
            first_devices = first.result()
            second_devices = second.result()

        self.assertEqual(set(first_devices) & set(second_devices), set())
        self.assertEqual(set(first_devices) | set(second_devices), {0, 1, 2, 3})

    def test_real_occupancy_wins_over_declarations(self) -> None:
        self.submit("task-busy", devices=[0])
        result = self.coordinator.acquire("task-busy", occupancy(busy=[0]))
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["reason"], "requested_devices_unavailable")
        self.assertEqual(result["unavailable_devices"], [0])

    def test_probe_failure_blocks_only_the_optional_grant(self) -> None:
        self.submit("task-probe")
        result = self.coordinator.acquire(
            "task-probe",
            {"status": "failed", "error": "npu-smi unavailable"},
        )
        self.assertEqual(result["status"], "probe_failed")
        snapshot = self.coordinator.snapshot(None, task_id="task-probe")
        self.assertEqual(snapshot["tasks"][0]["state"], "queued")

    def test_preflight_conflict_returns_grant_to_queue(self) -> None:
        self.submit("task-preflight", devices=[0])
        granted = self.coordinator.acquire("task-preflight", occupancy())
        token = granted["task"]["fence_token"]
        result = self.coordinator.preflight(
            "task-preflight",
            token,
            occupancy(busy=[0]),
        )
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["reason"], "external_occupancy")
        self.assertEqual(result["task"]["state"], "queued")
        self.assertIsNone(result["task"]["fence_token"])

    def test_future_manual_hold_blocks_overlapping_estimated_window(self) -> None:
        self.coordinator.add_hold(
            {
                "hold_id": "hold-human",
                "owner": "human-alice",
                "devices": [0],
                "not_before": self.clock() + 30,
                "duration_seconds": 120,
                "reason": "manual run",
            },
            occupancy(),
        )
        self.submit("task-held", devices=[0], estimated_duration_seconds=60)
        result = self.coordinator.acquire("task-held", occupancy())
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["unavailable_devices"], [0])

    def test_queue_and_unactivated_grant_expire(self) -> None:
        self.submit("task-queue", queue_ttl_seconds=5)
        self.clock.advance(6)
        snapshot = self.coordinator.snapshot(occupancy())
        task = next(item for item in snapshot["tasks"] if item["task_id"] == "task-queue")
        self.assertEqual(task["state"], "expired")

        self.submit("task-grant", queue_ttl_seconds=60)
        self.assertEqual(
            self.coordinator.acquire("task-grant", occupancy(), grant_ttl_seconds=5)["status"],
            "granted",
        )
        self.clock.advance(6)
        snapshot = self.coordinator.snapshot(occupancy())
        task = next(item for item in snapshot["tasks"] if item["task_id"] == "task-grant")
        self.assertEqual(task["state"], "expired")

    def test_heartbeat_timeout_keeps_busy_hardware_quarantined(self) -> None:
        self.submit("task-active", devices=[0])
        self.activate("task-active", heartbeat_ttl=5)
        self.clock.advance(6)

        busy_snapshot = self.coordinator.snapshot(occupancy(busy=[0]), task_id="task-active")
        self.assertEqual(busy_snapshot["tasks"][0]["state"], "orphaned_busy")

        free_snapshot = self.coordinator.snapshot(occupancy(), task_id="task-active")
        self.assertEqual(free_snapshot["tasks"][0]["state"], "released")

    def test_release_defers_while_hardware_is_busy(self) -> None:
        self.submit("task-release", devices=[0])
        token = self.activate("task-release")
        deferred = self.coordinator.release("task-release", token, occupancy(busy=[0]))
        self.assertEqual(deferred["status"], "orphaned_busy")
        released = self.coordinator.release("task-release", token, occupancy())
        self.assertEqual(released["status"], "released")

    def test_missing_device_is_unknown_not_free(self) -> None:
        self.submit("task-missing", devices=[0])
        token = self.activate("task-missing")
        result = self.coordinator.release(
            "task-missing",
            token,
            occupancy(devices=[1, 2, 3]),
        )
        self.assertEqual(result["status"], "orphaned_busy")
        self.assertEqual(result["conflicting_devices"], [0])

    def test_confirmation_probe_unions_transient_busy_samples(self) -> None:
        samples = iter([occupancy(busy=[0]), occupancy()])
        observed = _confirmed_free_probe(
            samples=2,
            interval_seconds=0,
            probe=lambda: next(samples),
        )
        self.assertIn("0", observed["busy"])
        self.assertNotIn(0, observed["free"])

    def test_npu_smi_parser_marks_process_and_hbm_occupancy(self) -> None:
        output = """
| 0     910B4      | OK              41.8        0                0 / 0 |
| 0     0          | 0000:C1:00.0     0            0 / 0       5000 / 65536 |
| 1     910B4      | OK              41.8        0                0 / 0 |
| 1     1          | 0000:81:00.0     0            0 / 0        100 / 65536 |
| NPU   Chip       | Process id      Process name             Process memory(MB) |
| 1     0            4321              root                     python             |
"""
        parsed = parse_npu_smi_info(output)
        self.assertEqual(parsed["devices"], [0, 1])
        self.assertEqual(sorted(parsed["busy"]), ["0", "1"])
        self.assertEqual(parsed["busy"]["0"][0]["kind"], "hbm_threshold")
        self.assertEqual(parsed["busy"]["1"][0]["pid"], 4321)


if __name__ == "__main__":
    unittest.main()
