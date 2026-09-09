#!/usr/bin/env python3
"""P21: convert remote-dev.result.v1 into envelope parts and children.

The twelve cells are the cross product of the six remote-dev outcomes and
the two envelope slots. Every converted object is placed in a wrapping
envelope and must pass ``validate_envelope``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from remote_dev.result import RESULT_SCHEMA_VERSION, make_result  # noqa: E402

from vaws_result_envelope import (  # noqa: E402
    CHILD_TO_REMOTE_DEV_OUTCOME,
    LOSSY_REMOTE_DEV_CHILD_OUTCOMES,
    LOSSY_REMOTE_DEV_PART_OUTCOMES,
    PART_TO_REMOTE_DEV_OUTCOME,
    REMOTE_DEV_OUTCOMES,
    REMOTE_DEV_RESULT_SCHEMA_VERSION,
    REMOTE_DEV_TO_CHILD_OUTCOME,
    REMOTE_DEV_TO_PART_OUTCOME,
    EnvelopeError,
    convert_remote_dev_result,
    failure_from_parts,
    make_attempt,
    make_command,
    make_environment,
    make_evidence,
    make_next_step,
    make_operation,
    new_envelope,
    outcome_from_parts,
    remote_dev_mapping_is_lossy,
    unknown_failure,
    validate_envelope,
)

NOW = "2026-09-09T12:00:00Z"
SLOTS = ("parts", "children")


def _remote_result(outcome: str) -> dict:
    return make_result(
        tool="bash",
        target={"kind": "container", "ref": "example.invalid:22"},
        outcome=outcome,  # type: ignore[arg-type]
        status=f"remote-{outcome}",
        summary=f"remote-dev {outcome} fixture",
        invocation_id=f"20260909T120000Z-{outcome[:8]}",
        started_at=NOW,
        duration_ms=12,
    )


def _wrap(converted: dict, slot: str) -> dict:
    command = make_command(argv=["python3", "-c", "pass"], cwd=".")
    kwargs: dict = {
        "operation": make_operation(
            entry_point=".agents/scripts/envelope_lint.py",
            action="convert-fixture",
            skill=None,
            target_kind="local",
        ),
        "summary": f"converted {slot} fixture",
        "attempt": make_attempt(
            command=command,
            reproduce=command["display"],
            started_at=NOW,
        ),
        "environment": make_environment(source="unknown"),
        "evidence": make_evidence(),
        "emitted_at": NOW,
        "envelope_id": f"convert-{slot}-20260909t120000z-abcd1234",
    }
    if slot == "parts":
        outcome = outcome_from_parts([converted])
        kwargs["parts"] = [converted]
        kwargs["outcome"] = outcome
        if outcome in {"failure", "blocked", "partial"}:
            kwargs["failure"] = failure_from_parts([converted]) or unknown_failure(
                message="converted part failed"
            )
            kwargs["next_step"] = make_next_step(actions=["inspect the converted part"])
        else:
            kwargs["next_step"] = make_next_step()
    else:
        child_outcome = str(converted["outcome"])
        kwargs["children"] = [converted]
        if child_outcome in {"failure", "blocked", "partial"}:
            kwargs["outcome"] = child_outcome
            kwargs["failure"] = unknown_failure(message=str(converted.get("summary")))
            kwargs["next_step"] = make_next_step(actions=["inspect the child digest"])
        elif child_outcome == "cancelled":
            kwargs["outcome"] = "cancelled"
            kwargs["next_step"] = make_next_step()
        else:
            kwargs["outcome"] = "success"
            kwargs["next_step"] = make_next_step()
    return new_envelope(**kwargs)


class MappingTableTests(unittest.TestCase):
    def test_schema_version_matches_installed_package(self) -> None:
        self.assertEqual(REMOTE_DEV_RESULT_SCHEMA_VERSION, RESULT_SCHEMA_VERSION)

    def test_tables_cover_every_remote_dev_outcome(self) -> None:
        self.assertEqual(set(REMOTE_DEV_TO_PART_OUTCOME), set(REMOTE_DEV_OUTCOMES))
        self.assertEqual(set(REMOTE_DEV_TO_CHILD_OUTCOME), set(REMOTE_DEV_OUTCOMES))

    def test_lossy_sets_are_exactly_the_renamed_cells(self) -> None:
        self.assertEqual(
            LOSSY_REMOTE_DEV_PART_OUTCOMES,
            {"failed", "timeout", "needs_input", "cancelled"},
        )
        self.assertEqual(
            LOSSY_REMOTE_DEV_CHILD_OUTCOMES,
            {"failed", "timeout", "needs_input"},
        )

    def test_reverse_maps_only_claim_unique_directions(self) -> None:
        self.assertEqual(PART_TO_REMOTE_DEV_OUTCOME["success"], "success")
        self.assertEqual(PART_TO_REMOTE_DEV_OUTCOME["skipped"], "cancelled")
        self.assertIsNone(PART_TO_REMOTE_DEV_OUTCOME["failure"])
        self.assertIsNone(PART_TO_REMOTE_DEV_OUTCOME["blocked"])
        self.assertEqual(CHILD_TO_REMOTE_DEV_OUTCOME["cancelled"], "cancelled")
        self.assertIsNone(CHILD_TO_REMOTE_DEV_OUTCOME["failure"])
        self.assertIsNone(CHILD_TO_REMOTE_DEV_OUTCOME["partial"])


class ConvertCrossProductTests(unittest.TestCase):
    def test_all_twelve_cells_validate(self) -> None:
        table: list[tuple[str, str, str, bool]] = []
        for outcome in sorted(REMOTE_DEV_OUTCOMES):
            for slot in SLOTS:
                result = _remote_result(outcome)
                converted = convert_remote_dev_result(
                    result,
                    slot=slot,
                    unit=f"unit-{outcome}",
                    envelope_id=f"child-{outcome}",
                    depth=1,
                    layer="unknown",
                    entry_point=".agents/lib/vaws_result_envelope.py",
                    action="convert",
                )
                envelope = _wrap(converted, slot)
                validate_envelope(envelope)
                mapped = (
                    REMOTE_DEV_TO_PART_OUTCOME[outcome]
                    if slot == "parts"
                    else REMOTE_DEV_TO_CHILD_OUTCOME[outcome]
                )
                self.assertEqual(converted["outcome"], mapped)
                lossy = remote_dev_mapping_is_lossy(outcome, slot)
                self.assertEqual(lossy, outcome != mapped)
                self.assertEqual(converted["evidence"]["remote_dev_outcome"], outcome)
                self.assertEqual(converted["evidence"]["lossy"], lossy)
                if lossy:
                    self.assertEqual(converted["evidence"]["remote_dev_outcome"], outcome)
                    self.assertNotEqual(converted["outcome"], outcome)
                if slot == "parts":
                    names = {item["name"] for item in converted["refs"]}
                    self.assertIn("remote_dev_outcome", names)
                    pointer = next(
                        item
                        for item in converted["refs"]
                        if item["name"] == "remote_dev_outcome"
                    )
                    self.assertEqual(pointer["ref"], outcome)
                table.append((outcome, slot, converted["outcome"], lossy))
        self.assertEqual(len(table), 12)

    def test_raw_paste_is_still_rejected(self) -> None:
        """The reason this conversion exists: raw paste fails every cell."""
        for outcome in sorted(REMOTE_DEV_OUTCOMES):
            raw = _remote_result(outcome)
            with self.subTest(outcome=outcome, slot="parts"):
                with self.assertRaises(EnvelopeError):
                    _wrap(raw, "parts")
            with self.subTest(outcome=outcome, slot="children"):
                with self.assertRaises(EnvelopeError):
                    _wrap(raw, "children")

    def test_caller_supplies_unit_and_envelope_id(self) -> None:
        result = _remote_result("success")
        with self.assertRaisesRegex(EnvelopeError, "unit"):
            convert_remote_dev_result(result, slot="parts")
        with self.assertRaisesRegex(EnvelopeError, "envelope_id"):
            convert_remote_dev_result(result, slot="children")

    def test_layer_is_not_inferred_as_transport(self) -> None:
        result = _remote_result("failed")
        part = convert_remote_dev_result(result, slot="parts", unit="bash-1")
        self.assertEqual(part["layer"], "unknown")
        child = convert_remote_dev_result(
            result, slot="children", envelope_id="child-failed"
        )
        self.assertEqual(child["layer"], "unknown")
        attributed = convert_remote_dev_result(
            result,
            slot="parts",
            unit="bash-2",
            layer="remote_workload",
            reason_code="workload_nonzero_exit",
        )
        self.assertEqual(attributed["layer"], "remote_workload")

    def test_wrong_schema_is_rejected(self) -> None:
        payload = _remote_result("success")
        payload["schema_version"] = "vaws.result-envelope.v1"
        with self.assertRaisesRegex(EnvelopeError, "remote-dev.result.v1"):
            convert_remote_dev_result(payload, slot="parts", unit="x")


class SkillPayloadConversionTests(unittest.TestCase):
    def test_embedded_remote_dev_result_becomes_a_part(self) -> None:
        from vaws_result_envelope import envelope_from_skill_payload

        remote = _remote_result("timeout")
        envelope = envelope_from_skill_payload(
            {
                "status": "failed",
                "error": "remote bash timed out",
                "remote_dev_result": remote,
            },
            skill="vllm-ascend-serving",
            entry_point=".agents/skills/vllm-ascend-serving/scripts/serve_status.py",
            action="serve_status",
            argv=["python3", ".agents/skills/vllm-ascend-serving/scripts/serve_status.py"],
            layer="transport",
        )
        validate_envelope(envelope)
        self.assertEqual(len(envelope["parts"]), 1)
        self.assertEqual(envelope["parts"][0]["outcome"], "failure")
        self.assertEqual(
            envelope["parts"][0]["evidence"]["remote_dev_outcome"], "timeout"
        )


if __name__ == "__main__":
    unittest.main()
