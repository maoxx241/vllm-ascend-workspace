from __future__ import annotations

import unittest

from npu_fleet_monitor.agent_view import (
    AgentQueryError, capacity_candidates, compact_server, find_server, npu_status, server_status,
)


SERVER = {
    "id": "server-1", "name": "atlas-a3", "host": "10.0.0.8", "port": 22,
    "enabled": True, "tags": ["A3"], "last_error": None,
}
SNAPSHOT = {
    "server_id": "server-1", "hostname": "atlas-host", "status": "online", "collected_at": 990,
    "summary": {
        "npu_count": 2, "busy_npu_count": 1, "npu_util_percent": 35,
        "hbm_used_mb": 49152, "hbm_total_mb": 131072,
    },
    "devices": [
        {
            "npu_id": 0, "name": "910B4", "aicore_percent": 70, "busy": True,
            "hbm": {"used_mb": 43008, "total_mb": 65536},
            "processes": [{
                "pid": 42, "name": "python3", "cwd": "/work/q00946761/wbj",
                "command": "python -m vllm", "npu_memory_mb": 32000,
                "container": {"name": "vllm-a3"},
                "ownership_labels": [{"value": "q00946761"}, {"value": "wbj"}],
            }],
        },
        {
            "npu_id": 1, "name": "910B4", "aicore_percent": 0, "busy": False,
            "hbm": {"used_mb": 6144, "total_mb": 65536}, "processes": [],
        },
    ],
}


class AgentViewTests(unittest.TestCase):
    def test_query_matches_ip_name_hostname_and_id(self) -> None:
        for query in ("10.0.0.8", "atlas-a3", "ATLAS-HOST", "server-1"):
            server, snapshot = find_server(query, [SERVER], {"server-1": SNAPSHOT})
            self.assertEqual(server["id"], "server-1")
            self.assertIs(snapshot, SNAPSHOT)

    def test_query_does_not_fuzzy_match_or_collect(self) -> None:
        with self.assertRaises(AgentQueryError) as raised:
            find_server("10.0.0", [SERVER], {"server-1": SNAPSHOT})
        self.assertEqual(raised.exception.status, 404)

    def test_compact_status_uses_cached_age(self) -> None:
        result = compact_server(SERVER, SNAPSHOT, now=1000)
        self.assertEqual(result["age_seconds"], 10)
        self.assertEqual(result["busy_npu_count"], 1)

    def test_npu_status_defaults_to_owner_summary(self) -> None:
        result = npu_status(SERVER, SNAPSHOT, now=1000)
        self.assertEqual(result["source"], "cache")
        self.assertEqual(result["summary"]["idle_npu_count"], 1)
        self.assertEqual(result["devices"][0]["hbm_percent"], 65.6)
        self.assertEqual(result["devices"][0]["owners"], ["q00946761", "wbj"])
        self.assertNotIn("processes", result["devices"][0])

    def test_detailed_processes_are_opt_in(self) -> None:
        result = npu_status(SERVER, SNAPSHOT, include_processes=True, detailed_processes=True, now=1000)
        process = result["devices"][0]["processes"][0]
        self.assertEqual(process["container"], "vllm-a3")
        self.assertEqual(process["cwd"], "/work/q00946761/wbj")
        self.assertEqual(process["command"], "python -m vllm")

    def test_capacity_filters_and_sorts_low_priority_last(self) -> None:
        regular = SERVER
        low = {**SERVER, "id": "server-2", "host": "10.0.0.9", "name": "low", "tags": ["低优先级"]}
        low_snapshot = {**SNAPSHOT, "server_id": "server-2", "summary": {**SNAPSHOT["summary"], "busy_npu_count": 0}}
        result = capacity_candidates(
            [low, regular], {"server-1": SNAPSHOT, "server-2": low_snapshot},
            min_idle_npus=1, max_age_seconds=30, now=1000,
        )
        self.assertEqual([item["host"] for item in result["candidates"]], ["10.0.0.8", "10.0.0.9"])

    def test_server_status_marks_likely_weight_mounts(self) -> None:
        snapshot = {
            **SNAPSHOT,
            "summary": {**SNAPSHOT["summary"], "memory_used_bytes": 50, "memory_total_bytes": 100},
            "mounts": [{"target": "/data/models", "source": "nfs:/weights", "fstype": "nfs4", "options": "rw"}],
            "disks": [{"mount": "/data/models", "total_bytes": 1000, "available_bytes": 600, "used_percent": 40}],
            "docker": {"available": True, "containers": []},
        }
        result = server_status(SERVER, snapshot, now=1000)
        self.assertEqual(result["system"]["memory_percent"], 50)
        self.assertTrue(result["storage"]["mounts"][0]["weight_candidate"])


if __name__ == "__main__":
    unittest.main()
