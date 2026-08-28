#!/usr/bin/env python3
"""Tests for structured distributed-debug evidence analysis."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "vllm-ascend-distributed-debug"


def load_module():
    name = "_distributed_debug_test"
    spec = importlib.util.spec_from_file_location(
        name, SKILL / "scripts" / "distributed_debug.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


distributed = load_module()
NOW = "2026-07-25T12:00:00Z"


def config() -> dict:
    ranks = []
    for rank in range(2):
        ranks.append(
            {
                "global_rank": rank,
                "node": "host-a",
                "device": rank,
                "local_rank": rank,
                "tp_rank": rank,
                "pp_rank": 0,
                "dp_rank": 0,
                "ep_rank": 0,
                "pcp_rank": 0,
                "dcp_rank": 0,
            }
        )
    return {
        "schema_version": 1,
        "run_id": "distributed-case-1",
        "expected_world_size": 2,
        "ranks": ranks,
        "groups": [{"name": "tp-0", "type": "tp", "ranks": [0, 1]}],
        "network_endpoints": [
            {"name": "master", "address": "10.0.0.1", "port": 29500}
        ],
        "environment": {"HCCL_BUFFSIZE": "1024"},
        "process_tree": {},
        "command": ["python", "repro.py"],
    }


def event(
    rank: int,
    kind: str,
    *,
    operation: str = "all_reduce",
    phase: str = "model-execute",
) -> dict:
    return {
        "timestamp": f"2026-07-25T12:00:0{rank}Z",
        "rank": rank,
        "phase": phase,
        "event": kind,
        "group": "tp-0",
        "sequence": 1,
        "operation": operation,
    }


class DistributedDebugTests(unittest.TestCase):
    def test_rejects_noncontiguous_rank_map(self) -> None:
        invalid = config()
        invalid["ranks"][1]["global_rank"] = 2
        with self.assertRaisesRegex(distributed.DistributedDebugError, "contiguous"):
            distributed.validate_config(invalid)

    def test_detects_collective_operation_mismatch(self) -> None:
        case = config()
        analysis = distributed.analyze_evidence(
            {
                "ranks": case["ranks"],
                "groups": case["groups"],
            },
            case["network_endpoints"],
            [
                event(0, "collective_enter", operation="all_reduce"),
                event(1, "collective_enter", operation="all_gather"),
            ],
        )
        self.assertEqual(analysis["status"], "diagnosed")
        self.assertIn(
            "collective-operation-mismatch", analysis["confirmed_findings"]
        )

    def test_detects_missing_collective_participant(self) -> None:
        case = config()
        analysis = distributed.analyze_evidence(
            {"ranks": case["ranks"], "groups": case["groups"]},
            case["network_endpoints"],
            [event(0, "collective_enter")],
        )
        self.assertIn(
            "collective-participant-mismatch", analysis["confirmed_findings"]
        )
        finding = next(
            row
            for row in analysis["findings"]
            if row["code"] == "collective-participant-mismatch"
        )
        self.assertEqual(finding["evidence"]["missing"], [1])

    def test_missing_rank_events_are_evidence_gap(self) -> None:
        case = config()
        analysis = distributed.analyze_evidence(
            {"ranks": case["ranks"], "groups": case["groups"]},
            case["network_endpoints"],
            [
                {
                    "timestamp": NOW,
                    "rank": 0,
                    "phase": "startup",
                    "event": "checkpoint",
                }
            ],
        )
        self.assertEqual(analysis["status"], "inconclusive")
        self.assertEqual(analysis["evidence_gaps"], ["missing-rank-evidence"])

    def test_full_lifecycle_writes_manifest_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "case"
            distributed.init_case(output, config_path=config_path, created_at=NOW)
            events_path = root / "events.jsonl"
            rows = []
            for kind in ("collective_enter", "collective_exit"):
                rows.extend([event(0, kind), event(1, kind)])
            events_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            distributed.ingest_events(
                output, events_path=events_path, updated_at=NOW
            )
            result = distributed.analyze_case(output, updated_at=NOW)
            self.assertEqual(result["status"], "no-mismatch-detected")
            self.assertTrue((output / "report.md").is_file())
            manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
