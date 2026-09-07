#!/usr/bin/env python3
"""Tests for Result Envelope v1, its taxonomy, redaction, and the lint.

Fixtures use RFC 5737 documentation addresses and ``*.invalid`` hostnames so
that a realistic-looking failure never puts a real endpoint in a tracked
file.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
SCRIPTS = ROOT / ".agents" / "scripts"
for candidate in (LIB, SCRIPTS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import envelope_lint  # noqa: E402
from vaws_result_envelope import (  # noqa: E402
    ENVIRONMENT_FIELDS,
    LAYERS,
    REDACTED_HOME,
    REDACTED_HOST,
    REDACTED_SECRET,
    SCHEMA_VERSION,
    EnvelopeError,
    EnvelopeRedactionError,
    assert_publishable,
    child_digest,
    compose_child,
    default_exit_code,
    dumps_publishable,
    emit,
    escalate_child_layer,
    evidence_ref,
    failure_from_parts,
    knowledge_reference,
    leak_findings,
    make_attempt,
    make_attempts,
    make_command,
    make_environment,
    make_evidence,
    make_failure,
    make_next_step,
    make_operation,
    make_part,
    make_remote_command,
    new_envelope,
    outcome_from_parts,
    progress,
    propagate_child_ruled_out,
    read_envelope,
    redact,
    text_preview,
    unknown_failure,
    validate_envelope,
)

NOW = "2026-09-04T12:00:00Z"
SCHEMA_PATH = ROOT / ".agents" / "schemas" / "result-envelope-v1.schema.json"


def base_command(*extra: str) -> dict:
    return make_command(
        argv=[
            "python3",
            ".agents/skills/vllm-ascend-serving/scripts/serve_start.py",
            *extra,
        ],
        cwd=".",
        env_keys=["ASCEND_RT_VISIBLE_DEVICES"],
        timeout_seconds=900,
    )


def base_envelope(**overrides) -> dict:
    command = overrides.pop("command", base_command("--session-id", "demo-1"))
    payload = {
        "operation": make_operation(
            entry_point=".agents/skills/vllm-ascend-serving/scripts/serve_start.py",
            action="serve_start",
            skill="vllm-ascend-serving",
            target_kind="session",
            target_id="demo-1",
        ),
        "outcome": "success",
        "summary": "service reached ready",
        "attempt": make_attempt(
            command=command,
            reproduce=command["display"],
            started_at=NOW,
            duration_ms=1234,
        ),
        "environment": make_environment(
            source="probe",
            captured_at=NOW,
            soc="ascend910_9391",
            cann="8.3.RC1",
            driver="25.2.0",
            torch="2.7.1",
            torch_npu="2.7.1.dev20260801",
            vllm="0.11.0",
            vllm_ascend="0.11.0rc1",
        ),
        "evidence": make_evidence(run_id="debug-20260904-120000-abcd1234"),
        "next_step": make_next_step(),
        "emitted_at": NOW,
        "envelope_id": "serve-start-20260904t120000z-abcd1234",
    }
    payload.update(overrides)
    return new_envelope(**payload)


class ConstructionTests(unittest.TestCase):
    def test_success_envelope_round_trips_through_json(self) -> None:
        envelope = base_envelope()
        reloaded = json.loads(json.dumps(envelope))
        validate_envelope(reloaded)
        self.assertEqual(reloaded["schema_version"], SCHEMA_VERSION)
        self.assertEqual(reloaded["exit_code"], 0)
        self.assertIsNone(reloaded["failure"])

    def test_environment_always_carries_every_field(self) -> None:
        environment = make_environment(source="declared", torch="2.7.1")
        for field in ENVIRONMENT_FIELDS:
            self.assertIn(field, environment)
        self.assertEqual(
            environment["unknown_fields"],
            sorted(set(ENVIRONMENT_FIELDS) - {"torch"}),
        )

    def test_environment_unknown_fields_must_match_null_versions(self) -> None:
        environment = make_environment(source="probe", torch="2.7.1")
        environment["unknown_fields"] = []
        with self.assertRaisesRegex(EnvelopeError, "unknown_fields"):
            base_envelope(environment=environment)

    def test_absolute_entry_point_is_rejected(self) -> None:
        with self.assertRaisesRegex(EnvelopeError, "repo-relative"):
            base_envelope(
                operation=make_operation(
                    entry_point="/opt/vaws/.agents/scripts/remote_exec.py",
                    action="remote_exec",
                )
            )

    def test_unknown_top_level_field_is_rejected(self) -> None:
        envelope = base_envelope()
        envelope["metrics"] = {"throughput": 1.0}
        with self.assertRaisesRegex(EnvelopeError, "unknown top-level fields"):
            validate_envelope(envelope)

    def test_extensions_is_the_additive_escape_hatch(self) -> None:
        envelope = base_envelope(extensions={"metrics": {"throughput": 1.0}})
        validate_envelope(envelope)
        self.assertEqual(envelope["extensions"]["metrics"]["throughput"], 1.0)

    def test_default_exit_codes_distinguish_outcomes(self) -> None:
        self.assertEqual(default_exit_code("success"), 0)
        self.assertEqual(default_exit_code("failure"), 1)
        self.assertEqual(default_exit_code("blocked"), 2)
        self.assertEqual(default_exit_code("cancelled"), 3)


class TaxonomyTests(unittest.TestCase):
    def test_every_layer_has_a_worked_failure(self) -> None:
        """One envelope per layer, mirroring docs/agent-feedback-contract.md."""
        fixtures = {
            "caller": ("bad_arguments", "--tp 4 conflicts with --devices 0,1"),
            "tool": (
                "tool_service_instant_timeout",
                "remote_bash returned timeout in 40ms for every command",
            ),
            "transport": (
                "ssh_mux_stream_died",
                "ssh stream closed immediately over the shared ControlMaster",
            ),
            "remote_env": (
                "hostname_unresolvable",
                "gloo could not resolve the container hostname",
            ),
            "remote_workload": (
                "workload_nonzero_exit",
                "vllm serve exited 1 during model load",
            ),
            "device": ("npu_busy", "devices 0-3 already held by another lease"),
            "unknown": (
                "unattributed",
                "service never bound the port and no log was produced",
            ),
        }
        self.assertEqual(set(fixtures), set(LAYERS))
        for layer, (reason_code, message) in fixtures.items():
            with self.subTest(layer=layer):
                failure = (
                    unknown_failure(message=message)
                    if layer == "unknown"
                    else make_failure(
                        layer=layer,
                        reason_code=reason_code,
                        message=message,
                        attribution_basis=[f"observed: {message}"],
                        confidence="high",
                    )
                )
                envelope = base_envelope(
                    outcome="failure",
                    summary=message[:200],
                    failure=failure,
                    next_step=make_next_step(
                        actions=["read the referenced log"],
                    ),
                )
                validate_envelope(envelope)
                self.assertEqual(envelope["failure"]["layer"], layer)
                self.assertEqual(envelope["exit_code"], 1)

    def test_layer_claim_without_evidence_is_rejected(self) -> None:
        failure = make_failure(
            layer="remote_workload",
            reason_code="workload_nonzero_exit",
            message="it broke",
        )
        with self.assertRaisesRegex(EnvelopeError, "attribution_basis"):
            base_envelope(
                outcome="failure",
                failure=failure,
                next_step=make_next_step(actions=["look at the log"]),
            )

    def test_unknown_layer_needs_no_basis_but_cannot_be_confident(self) -> None:
        envelope = base_envelope(
            outcome="failure",
            failure=unknown_failure(message="no usable signal"),
            next_step=make_next_step(
                actions=["re-run with --timeout 1800 and keep the stderr log"]
            ),
        )
        validate_envelope(envelope)
        self.assertEqual(envelope["failure"]["confidence"], "low")
        bad = unknown_failure(message="no usable signal")
        bad["confidence"] = "high"
        with self.assertRaisesRegex(EnvelopeError, "cannot be high"):
            base_envelope(
                outcome="failure",
                failure=bad,
                next_step=make_next_step(actions=["collect more evidence"]),
            )

    def test_ruled_out_cannot_contain_the_claimed_layer(self) -> None:
        failure = make_failure(
            layer="transport",
            reason_code="ssh_connect_failed",
            message="connection refused",
            attribution_basis=["ssh exited 255 before the remote shell started"],
            ruled_out=["transport"],
        )
        with self.assertRaisesRegex(EnvelopeError, "must not contain"):
            base_envelope(
                outcome="failure",
                failure=failure,
                next_step=make_next_step(actions=["check the container port"]),
            )

    def test_failure_requires_a_next_step(self) -> None:
        with self.assertRaisesRegex(EnvelopeError, "next_step action"):
            base_envelope(
                outcome="failure",
                failure=unknown_failure(message="no usable signal"),
            )

    def test_success_must_not_carry_a_failure(self) -> None:
        with self.assertRaisesRegex(EnvelopeError, "must not carry a failure"):
            base_envelope(failure=unknown_failure(message="stray"))

    def test_known_signature_records_what_not_to_look_at(self) -> None:
        envelope = base_envelope(
            outcome="failure",
            summary="gloo init failed on a fresh container",
            failure=make_failure(
                layer="remote_env",
                reason_code="hostname_unresolvable",
                message="gloo::makeDeviceForHostname failed for the container",
                attribution_basis=[
                    "stderr matches a recorded known-failure signature",
                ],
                confidence="high",
                ruled_out=["remote_workload", "device"],
            ),
            next_step=make_next_step(
                actions=[
                    {
                        "description": "map the container hostname in /etc/hosts",
                        "command": "echo \"127.0.0.1 $(hostname)\" >> /etc/hosts",
                    }
                ],
                do_not=[
                    "do not tune GLOO_SOCKET_IFNAME or other HCCL env vars "
                    "before /etc/hosts is fixed",
                ],
                knowledge=[
                    knowledge_reference(
                        entry_id="gloo-init-container-hostname-missing-from-etc-hosts",
                        summary="fresh containers lack their own hostname mapping",
                        avoidance="do not debug gloo/HCCL env vars first",
                        score=7,
                    )
                ],
            ),
        )
        validate_envelope(envelope)
        self.assertTrue(envelope["next_step"]["do_not"])
        self.assertEqual(
            envelope["next_step"]["knowledge"][0]["entry_id"],
            "gloo-init-container-hostname-missing-from-etc-hosts",
        )


class PartialSuccessTests(unittest.TestCase):
    def _parts(self) -> list[dict]:
        return [
            make_part(unit="node-0", outcome="success"),
            make_part(unit="node-1", outcome="success"),
            make_part(unit="node-2", outcome="success"),
            make_part(
                unit="node-3",
                outcome="failure",
                layer="device",
                reason_code="npu_busy",
                summary="devices 4-7 held by another lease",
            ),
        ]

    def test_three_of_four_nodes_is_partial_not_boolean(self) -> None:
        parts = self._parts()
        self.assertEqual(outcome_from_parts(parts), "partial")
        failure = failure_from_parts(parts)
        envelope = base_envelope(
            outcome="partial",
            summary="3/4 nodes started, node-3 blocked on devices",
            failure=failure,
            parts=parts,
            next_step=make_next_step(actions=["release the node-3 device lease"]),
        )
        validate_envelope(envelope)
        self.assertEqual(envelope["failure"]["layer"], "device")
        self.assertEqual(envelope["exit_code"], 1)
        self.assertEqual(len(envelope["parts"]), 4)

    def test_mixed_part_layers_aggregate_to_unknown(self) -> None:
        parts = [
            make_part(unit="node-0", outcome="success"),
            make_part(
                unit="node-1", outcome="failure", layer="device", reason_code="npu_busy"
            ),
            make_part(
                unit="node-2",
                outcome="failure",
                layer="transport",
                reason_code="ssh_connect_failed",
            ),
        ]
        failure = failure_from_parts(parts)
        self.assertEqual(failure["layer"], "unknown")

    def test_outcome_must_agree_with_parts(self) -> None:
        with self.assertRaisesRegex(EnvelopeError, "disagrees with parts"):
            base_envelope(
                outcome="failure",
                failure=failure_from_parts(self._parts()),
                parts=self._parts(),
                next_step=make_next_step(actions=["inspect node-3"]),
            )

    def test_failing_part_must_carry_a_layer(self) -> None:
        parts = [
            make_part(unit="node-0", outcome="success"),
            make_part(unit="node-1", outcome="failure"),
        ]
        with self.assertRaisesRegex(EnvelopeError, "must carry a layer"):
            base_envelope(
                outcome="partial",
                failure=unknown_failure(message="node-1 failed"),
                parts=parts,
                next_step=make_next_step(actions=["inspect node-1"]),
            )

    def test_partial_without_parts_is_rejected(self) -> None:
        with self.assertRaisesRegex(EnvelopeError, "requires parts"):
            base_envelope(
                outcome="partial",
                failure=unknown_failure(message="something partially failed"),
                next_step=make_next_step(actions=["inspect the units"]),
            )

    def test_all_units_blocked_is_blocked_not_partial(self) -> None:
        parts = [
            make_part(unit="node-0", outcome="blocked", layer="caller"),
            make_part(unit="node-1", outcome="blocked", layer="caller"),
        ]
        self.assertEqual(outcome_from_parts(parts), "blocked")

    def test_one_unknown_failure_stays_unknown_with_low_confidence(self) -> None:
        parts = [
            make_part(
                unit="node-0",
                outcome="failure",
                layer="unknown",
                reason_code="fixture",
            )
        ]
        failure = failure_from_parts(parts)
        self.assertEqual(failure["layer"], "unknown")
        self.assertEqual(failure["confidence"], "low")
        self.assertIn("node-0", failure["attribution_basis"][0])
        envelope = base_envelope(
            outcome="failure",
            failure=failure,
            parts=parts,
            next_step=make_next_step(actions=["inspect fixture log"]),
        )
        validate_envelope(envelope)
        self.assertEqual(envelope["outcome"], "failure")

    def test_several_unknown_failures_do_not_raise_confidence(self) -> None:
        parts = [
            make_part(
                unit="node-0",
                outcome="failure",
                layer="unknown",
                reason_code="fixture",
            ),
            make_part(
                unit="node-1",
                outcome="failure",
                layer="unknown",
                reason_code="fixture",
            ),
            make_part(
                unit="node-2",
                outcome="failure",
                layer="unknown",
                reason_code="fixture",
            ),
        ]
        failure = failure_from_parts(parts)
        self.assertEqual(failure["layer"], "unknown")
        self.assertEqual(failure["confidence"], "low")
        self.assertIn("node-0", failure["attribution_basis"][0])
        self.assertIn("node-2", failure["attribution_basis"][0])
        envelope = base_envelope(
            outcome="failure",
            failure=failure,
            parts=parts,
            next_step=make_next_step(actions=["collect a layer signal"]),
        )
        validate_envelope(envelope)
        self.assertEqual(envelope["outcome"], "failure")

    def test_all_unknown_blocked_parts_stay_blocked_and_low_confidence(self) -> None:
        parts = [
            make_part(
                unit="node-0",
                outcome="blocked",
                layer="unknown",
                reason_code="fixture",
            ),
            make_part(
                unit="node-1",
                outcome="blocked",
                layer="unknown",
                reason_code="fixture",
            ),
        ]
        self.assertEqual(outcome_from_parts(parts), "blocked")
        failure = failure_from_parts(parts)
        self.assertEqual(failure["layer"], "unknown")
        self.assertEqual(failure["confidence"], "low")
        envelope = base_envelope(
            outcome="blocked",
            failure=failure,
            parts=parts,
            next_step=make_next_step(actions=["collect a layer signal"]),
        )
        validate_envelope(envelope)
        self.assertEqual(envelope["outcome"], "blocked")

    def test_known_same_layer_failures_keep_high_confidence(self) -> None:
        parts = [
            make_part(
                unit="node-0",
                outcome="failure",
                layer="tool",
                reason_code="fixture",
            )
        ]
        failure = failure_from_parts(parts)
        self.assertEqual(failure["layer"], "tool")
        self.assertEqual(failure["confidence"], "high")
        envelope = base_envelope(
            outcome="failure",
            failure=failure,
            parts=parts,
            next_step=make_next_step(actions=["inspect fixture log"]),
        )
        validate_envelope(envelope)
        self.assertEqual(envelope["outcome"], "failure")

    def test_mixed_attributed_and_unknown_parts_stay_unknown_and_low(self) -> None:
        parts = [
            make_part(
                unit="node-0",
                outcome="failure",
                layer="device",
                reason_code="npu_busy",
            ),
            make_part(
                unit="node-1",
                outcome="failure",
                layer="unknown",
                reason_code="fixture",
            ),
        ]
        self.assertEqual(outcome_from_parts(parts), "failure")
        failure = failure_from_parts(parts)
        self.assertEqual(failure["layer"], "unknown")
        self.assertEqual(failure["confidence"], "low")
        envelope = base_envelope(
            outcome="failure",
            failure=failure,
            parts=parts,
            next_step=make_next_step(
                actions=["separate the attributed unit from the rest"]
            ),
        )
        validate_envelope(envelope)
        self.assertEqual(envelope["outcome"], "failure")

    def test_partial_unknown_failures_stay_partial_and_low_confidence(self) -> None:
        parts = [
            make_part(unit="node-0", outcome="success"),
            make_part(
                unit="node-1",
                outcome="failure",
                layer="unknown",
                reason_code="fixture",
            ),
        ]
        self.assertEqual(outcome_from_parts(parts), "partial")
        failure = failure_from_parts(parts)
        self.assertEqual(failure["layer"], "unknown")
        self.assertEqual(failure["confidence"], "low")
        envelope = base_envelope(
            outcome="partial",
            failure=failure,
            parts=parts,
            next_step=make_next_step(actions=["inspect node-1"]),
        )
        validate_envelope(envelope)
        self.assertEqual(envelope["outcome"], "partial")


class RetryTests(unittest.TestCase):
    def test_retry_safe_is_derived_from_the_idempotency_class(self) -> None:
        self.assertIs(
            make_attempts(idempotency_class="idempotent")["idempotency"]["retry_safe"],
            True,
        )
        self.assertIs(
            make_attempts(idempotency_class="unsafe_to_retry")["idempotency"][
                "retry_safe"
            ],
            False,
        )
        self.assertIsNone(
            make_attempts(idempotency_class="unknown")["idempotency"]["retry_safe"]
        )
        self.assertIsNone(
            make_attempts(idempotency_class="at_most_once")["idempotency"][
                "retry_safe"
            ]
        )

    def test_hand_edited_retry_safe_is_rejected(self) -> None:
        attempts = make_attempts(idempotency_class="unknown")
        attempts["idempotency"]["retry_safe"] = True
        with self.assertRaisesRegex(EnvelopeError, "derived from class"):
            base_envelope(attempts=attempts)

    def test_retry_history_cannot_exceed_the_attempt_count(self) -> None:
        attempts = make_attempts(
            count=1,
            records=[
                {"index": 1, "outcome": "failure"},
                {"index": 2, "outcome": "success"},
            ],
        )
        with self.assertRaisesRegex(EnvelopeError, "not be longer"):
            base_envelope(attempts=attempts)

    def test_retry_history_records_the_per_attempt_layer(self) -> None:
        attempts = make_attempts(
            count=2,
            records=[
                {
                    "index": 1,
                    "outcome": "failure",
                    "layer": "transport",
                    "reason_code": "ssh_mux_stream_died",
                },
                {"index": 2, "outcome": "success"},
            ],
            idempotency_class="idempotent",
            side_effects=["none: the probe only reads"],
        )
        envelope = base_envelope(attempts=attempts)
        validate_envelope(envelope)
        self.assertEqual(envelope["attempts"]["records"][0]["layer"], "transport")


class CompositionTests(unittest.TestCase):
    def _child(
        self,
        layer: str,
        reason_code: str,
        *,
        ruled_out: list[str] | None = None,
        outcome: str = "failure",
    ) -> dict:
        command = make_command(
            argv=["python3", ".agents/scripts/remote_exec.py", "--session-id", "demo-1"]
        )
        return new_envelope(
            operation=make_operation(
                entry_point=".agents/scripts/remote_exec.py",
                action="remote_exec",
                target_kind="container",
                target_id="demo-1",
            ),
            outcome=outcome,
            summary=f"remote_exec failed in layer {layer}",
            attempt=make_attempt(
                command=command, reproduce=command["display"], started_at=NOW
            ),
            failure=make_failure(
                layer=layer,
                reason_code=reason_code,
                message="nested failure",
                attribution_basis=["nested observation"],
                ruled_out=ruled_out,
            ),
            next_step=make_next_step(
                actions=["read the nested log"],
                do_not=["do not retry on the shared mux"],
            ),
            emitted_at=NOW,
            envelope_id="remote-exec-20260904t120000z-11112222",
        )

    def test_child_digest_is_bounded(self) -> None:
        digest = child_digest(self._child("transport", "ssh_mux_stream_died"))
        self.assertNotIn("children", digest)
        self.assertNotIn("attempt", digest)
        self.assertEqual(digest["layer"], "transport")

    def test_propagate_child_ruled_out_is_frame_aware(self) -> None:
        self.assertEqual(
            propagate_child_ruled_out(
                ["tool", "device"],
                child_layer="caller",
                parent_layer="tool",
            ),
            ["device"],
        )
        self.assertEqual(
            propagate_child_ruled_out(
                ["tool", "device"],
                child_layer="transport",
                parent_layer="transport",
            ),
            ["tool", "device"],
        )
        self.assertEqual(
            escalate_child_layer("caller"),
            "tool",
        )

    def test_nested_layer_propagates_unchanged(self) -> None:
        parent = base_envelope()
        child = self._child("remote_workload", "workload_nonzero_exit")
        composed = compose_child(parent, child, ref=".vaws-local/x/child.json")
        self.assertEqual(composed["outcome"], "failure")
        self.assertEqual(composed["failure"]["layer"], "remote_workload")
        self.assertEqual(composed["children"][0]["depth"], 1)

    def test_nested_caller_fault_becomes_the_parents_tool_fault(self) -> None:
        self.assertEqual(escalate_child_layer("caller"), "tool")
        parent = base_envelope()
        composed = compose_child(parent, self._child("caller", "bad_arguments"))
        self.assertEqual(composed["failure"]["layer"], "tool")
        self.assertTrue(
            any(
                "built the arguments" in item
                for item in composed["failure"]["attribution_basis"]
            )
        )

    def test_composition_inherits_child_do_not_guidance(self) -> None:
        parent = base_envelope()
        composed = compose_child(
            parent, self._child("transport", "ssh_mux_stream_died")
        )
        self.assertIn(
            "do not retry on the shared mux", composed["next_step"]["do_not"]
        )

    def test_parent_with_its_own_failure_keeps_it(self) -> None:
        parent = base_envelope(
            outcome="failure",
            failure=make_failure(
                layer="tool",
                reason_code="non_json_output",
                message="child returned non-JSON stdout",
                attribution_basis=["stdout did not parse as JSON"],
            ),
            next_step=make_next_step(actions=["read the raw child stdout"]),
        )
        composed = compose_child(
            parent, self._child("remote_workload", "workload_nonzero_exit")
        )
        self.assertEqual(composed["failure"]["layer"], "tool")

    def test_nested_envelope_in_children_is_rejected(self) -> None:
        parent = base_envelope()
        parent["children"] = [self._child("transport", "ssh_connect_failed")]
        with self.assertRaisesRegex(EnvelopeError, "must be a digest"):
            validate_envelope(parent)

    def test_nested_caller_without_child_tool_exclusion_composes(self) -> None:
        parent = base_envelope()
        child = self._child("caller", "bad_arguments", outcome="blocked")
        child_snapshot = json.loads(json.dumps(child))
        validate_envelope(parent)
        validate_envelope(child)
        composed = compose_child(parent, child, ref="fixture-child.json")
        validate_envelope(composed)
        self.assertEqual(composed["outcome"], "blocked")
        self.assertEqual(composed["failure"]["layer"], "tool")
        self.assertEqual(composed["failure"]["ruled_out"], [])
        self.assertEqual(child, child_snapshot)
        self.assertEqual(composed["children"][0]["envelope_id"], child["envelope_id"])
        self.assertEqual(composed["children"][0]["ref"], "fixture-child.json")
        self.assertEqual(composed["children"][0]["layer"], "caller")
        self.assertEqual(
            composed["next_step"]["actions"][0]["description"],
            "read the nested log",
        )
        self.assertIn(
            "do not retry on the shared mux", composed["next_step"]["do_not"]
        )

    def test_nested_caller_with_child_tool_exclusion_drops_parent_tool(self) -> None:
        parent = base_envelope()
        child = self._child(
            "caller",
            "bad_arguments",
            ruled_out=["tool"],
            outcome="blocked",
        )
        child_snapshot = json.loads(json.dumps(child))
        validate_envelope(parent)
        validate_envelope(child)
        composed = compose_child(parent, child, ref="fixture-child.json")
        validate_envelope(composed)
        self.assertEqual(composed["outcome"], "blocked")
        self.assertEqual(composed["failure"]["layer"], "tool")
        self.assertNotIn("tool", composed["failure"]["ruled_out"])
        self.assertEqual(child, child_snapshot)
        self.assertEqual(child["failure"]["layer"], "caller")
        self.assertEqual(child["failure"]["ruled_out"], ["tool"])
        self.assertEqual(composed["children"][0]["envelope_id"], child["envelope_id"])
        self.assertEqual(composed["children"][0]["ref"], "fixture-child.json")
        self.assertEqual(composed["children"][0]["layer"], "caller")
        self.assertEqual(
            composed["next_step"]["actions"][0]["description"],
            "read the nested log",
        )
        self.assertIn(
            "do not retry on the shared mux", composed["next_step"]["do_not"]
        )

    def test_nested_caller_keeps_downstream_exclusions(self) -> None:
        parent = base_envelope()
        child = self._child(
            "caller",
            "bad_arguments",
            ruled_out=["tool", "transport", "device"],
        )
        child_snapshot = json.loads(json.dumps(child))
        composed = compose_child(parent, child, ref="fixture-child.json")
        validate_envelope(composed)
        self.assertEqual(composed["failure"]["layer"], "tool")
        self.assertEqual(composed["failure"]["ruled_out"], ["transport", "device"])
        self.assertEqual(
            child["failure"]["ruled_out"], ["tool", "transport", "device"]
        )
        self.assertEqual(child, child_snapshot)
        self.assertEqual(composed["children"][0]["layer"], "caller")
        self.assertEqual(composed["children"][0]["ref"], "fixture-child.json")

    def test_unchanged_layer_keeps_child_exclusions(self) -> None:
        parent = base_envelope()
        child = self._child(
            "transport",
            "ssh_mux_stream_died",
            ruled_out=["caller", "tool"],
        )
        child_snapshot = json.loads(json.dumps(child))
        composed = compose_child(parent, child, ref="fixture-child.json")
        validate_envelope(composed)
        self.assertEqual(composed["failure"]["layer"], "transport")
        self.assertEqual(composed["failure"]["ruled_out"], ["caller", "tool"])
        self.assertEqual(child, child_snapshot)
        self.assertEqual(composed["children"][0]["layer"], "transport")
        self.assertEqual(composed["children"][0]["ref"], "fixture-child.json")
        self.assertIn(
            "do not retry on the shared mux", composed["next_step"]["do_not"]
        )


class BoundedOutputTests(unittest.TestCase):
    def test_short_text_is_inlined(self) -> None:
        preview = text_preview("short output")
        self.assertFalse(preview["truncated"])
        self.assertEqual(preview["text"], "short output")

    def test_truncated_preview_without_a_ref_is_rejected(self) -> None:
        preview = text_preview("x" * 20000)
        self.assertTrue(preview["truncated"])
        with self.assertRaisesRegex(EnvelopeError, "must carry a ref"):
            base_envelope(evidence=make_evidence(previews={"stderr": preview}))

    def test_truncated_preview_with_a_ref_is_accepted(self) -> None:
        preview = text_preview(
            "x" * 20000, ref=".vaws-local/serving/demo-1/stderr.log"
        )
        envelope = base_envelope(
            evidence=make_evidence(
                previews={"stderr": preview},
                refs=[
                    evidence_ref(
                        name="stderr",
                        kind="log",
                        ref=".vaws-local/serving/demo-1/stderr.log",
                        bytes_=20000,
                    )
                ],
                run_id="profiling-20260904-120000-deadbeef",
                manifest_ref=".vaws-local/profiling/runs/demo/manifest.json",
            )
        )
        validate_envelope(envelope)
        self.assertEqual(envelope["evidence"]["refs"][0]["kind"], "log")

    def test_summary_is_length_capped(self) -> None:
        with self.assertRaisesRegex(EnvelopeError, "at most"):
            base_envelope(summary="x" * 401)


class ReproducibilityTests(unittest.TestCase):
    def test_remote_command_needs_argv_or_a_script_preview(self) -> None:
        remote = make_remote_command(
            endpoint_kind="container", endpoint_ref="session:demo-1"
        )
        command = base_command()
        with self.assertRaisesRegex(EnvelopeError, "reproducible by hand"):
            base_envelope(
                attempt=make_attempt(
                    command=command,
                    reproduce=command["display"],
                    remote_command=remote,
                    started_at=NOW,
                )
            )

    def test_remote_script_preview_carries_a_ref_when_truncated(self) -> None:
        remote = make_remote_command(
            endpoint_kind="container",
            endpoint_ref="session:demo-1",
            script="echo hello\n" * 2000,
            script_ref=".vaws-local/remote-toolbox/logs/exec-1/script.sh",
            cwd="/vllm-workspace",
            env_keys=["ASCEND_RT_VISIBLE_DEVICES"],
            timeout_seconds=600,
        )
        command = base_command()
        envelope = base_envelope(
            attempt=make_attempt(
                command=command,
                reproduce=command["display"],
                remote_command=remote,
                started_at=NOW,
            )
        )
        validate_envelope(envelope)
        self.assertTrue(
            envelope["attempt"]["remote_command"]["script_preview"]["truncated"]
        )

    def test_reproduce_is_a_single_copy_pasteable_string(self) -> None:
        command = make_command(
            argv=[
                "python3",
                ".agents/skills/vllm-ascend-benchmark/scripts/bench_run.py",
                "--model",
                "/home/weights/Model With Space",
            ]
        )
        self.assertIn("'/home/weights/Model With Space'", command["display"])

    def test_empty_argv_is_rejected(self) -> None:
        command = make_command(argv=[])
        with self.assertRaisesRegex(EnvelopeError, "argv must not be empty"):
            base_envelope(
                attempt=make_attempt(
                    command=command, reproduce="n/a", started_at=NOW
                )
            )


class RedactionTests(unittest.TestCase):
    def _identity_envelope(self) -> dict:
        command = make_command(
            argv=[
                "ssh",
                "-p",
                "22",
                "root@198.51.100.7",
                "python3",
                "/home/example-user/work/run.py",
            ]
        )
        return base_envelope(
            outcome="failure",
            summary="ssh to the container failed",
            command=command,
            attempt=make_attempt(
                command=command,
                reproduce=command["display"],
                started_at=NOW,
            ),
            failure=make_failure(
                layer="transport",
                reason_code="ssh_connect_failed",
                message="ssh: connect to host 198.51.100.7 port 22: refused",
                attribution_basis=["ssh exited 255 before the remote shell ran"],
            ),
            next_step=make_next_step(actions=["verify the container ssh port"]),
            extensions={"env": {"HF_TOKEN": "hf_realsecretvaluegoeshere"}},
        )

    def test_routable_host_and_home_path_are_redacted(self) -> None:
        envelope = self._identity_envelope()
        self.assertTrue(leak_findings(envelope))
        redacted = redact(envelope)
        text = json.dumps(redacted)
        self.assertNotIn("198.51.100.7", text)
        self.assertNotIn("/home/example-user", text)
        self.assertIn(REDACTED_HOST, text)
        self.assertIn(REDACTED_HOME, text)
        self.assertEqual(leak_findings(redacted), [])

    def test_secret_like_keys_are_replaced_not_previewed(self) -> None:
        redacted = redact(self._identity_envelope())
        self.assertEqual(
            redacted["extensions"]["env"]["HF_TOKEN"], REDACTED_SECRET
        )

    def test_documentation_addresses_are_redacted_too(self) -> None:
        """Redaction does not reason about whether an address is 'safe'."""
        command = make_command(
            argv=["ssh", "root@192.0.2.10", "true"],
        )
        envelope = base_envelope(
            command=command,
            attempt=make_attempt(
                command=command, reproduce=command["display"], started_at=NOW
            ),
        )
        redacted = redact(envelope)
        self.assertNotIn("192.0.2.10", json.dumps(redacted))
        assert_publishable(redacted)

    def test_shared_weight_paths_survive_redaction(self) -> None:
        """Model paths name no user and are load-bearing for comparison."""
        command = make_command(
            argv=[
                "python3",
                ".agents/skills/vllm-ascend-benchmark/scripts/bench_run.py",
                "--model",
                "/home/weights/Qwen3.5-35B",
            ]
        )
        envelope = base_envelope(
            command=command,
            attempt=make_attempt(
                command=command, reproduce=command["display"], started_at=NOW
            ),
        )
        redacted = redact(envelope)
        self.assertIn("/home/weights/Qwen3.5-35B", json.dumps(redacted))
        assert_publishable(redacted)

    def test_loopback_and_invalid_hostnames_are_publishable(self) -> None:
        command = make_command(
            argv=["curl", "http://127.0.0.1:8000/health"],
        )
        envelope = base_envelope(
            command=command,
            attempt=make_attempt(
                command=command, reproduce=command["display"], started_at=NOW
            ),
            extensions={"base_url": "http://container-1.example.invalid:8000"},
        )
        assert_publishable(envelope)

    def test_publishable_dump_refuses_a_surviving_leak(self) -> None:
        envelope = base_envelope(extensions={"note": "see 2001:db8:1:2::5 host"})
        with self.assertRaises(EnvelopeRedactionError):
            assert_publishable(envelope)
        # A pattern redaction cannot reach is caught at serialization time.
        self.assertIn(
            "<redacted-host>", dumps_publishable(envelope)
        )

    def test_assert_publishable_reports_a_json_path(self) -> None:
        envelope = base_envelope(extensions={"host": "203.0.113.9"})
        with self.assertRaises(EnvelopeRedactionError) as ctx:
            assert_publishable(envelope)
        self.assertIn("$.extensions.host", str(ctx.exception))


class VersioningTests(unittest.TestCase):
    def test_same_version_is_validated_strictly(self) -> None:
        envelope = base_envelope()
        view = read_envelope(envelope)
        self.assertEqual(view["compat_warnings"], [])

    def test_future_version_is_read_leniently(self) -> None:
        envelope = base_envelope(
            outcome="failure",
            failure=make_failure(
                layer="transport",
                reason_code="ssh_connect_failed",
                message="refused",
                attribution_basis=["ssh exited 255"],
            ),
            next_step=make_next_step(actions=["check the port"]),
        )
        future = json.loads(json.dumps(envelope))
        future["schema_version"] = "vaws.result-envelope.v2"
        future["outcome"] = "degraded"
        future["failure"]["layer"] = "scheduler"
        view = read_envelope(future)
        self.assertEqual(view["outcome"], "failure")
        self.assertEqual(view["failure"]["layer"], "unknown")
        self.assertEqual(len(view["compat_warnings"]), 3)

    def test_schema_document_matches_the_library_contract(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["properties"]["schema_version"]["const"], SCHEMA_VERSION
        )
        self.assertEqual(
            set(schema["required"]), set(base_envelope().keys())
        )
        self.assertEqual(
            schema["$defs"]["layer"]["enum"], list(LAYERS)
        )
        self.assertEqual(
            set(schema["properties"]["environment"]["required"])
            - {"source", "captured_at", "unknown_fields"},
            set(ENVIRONMENT_FIELDS),
        )

    def test_schema_document_validates_with_jsonschema_when_available(self) -> None:
        try:
            import jsonschema  # noqa: PLC0415
        except ImportError:
            self.skipTest("jsonschema is not installed; library validation is authoritative")
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(base_envelope(), schema)


class EmissionTests(unittest.TestCase):
    def test_emit_writes_one_line_delimited_object_and_returns_exit_code(self) -> None:
        stream = io.StringIO()
        envelope = base_envelope()
        code = emit(envelope, stream=stream)
        self.assertEqual(code, 0)
        payload = stream.getvalue()
        self.assertTrue(payload.endswith("\n"))
        self.assertEqual(json.loads(payload)["envelope_id"], envelope["envelope_id"])

    def test_progress_goes_to_the_given_stderr_stream(self) -> None:
        stream = io.StringIO()
        progress("probe", "waiting for ready", stream=stream, port=8000)
        line = stream.getvalue().strip()
        self.assertTrue(line.startswith("__VAWS_PROGRESS__="))
        payload = json.loads(line.split("=", 1)[1])
        self.assertEqual(payload["port"], 8000)


class LintTests(unittest.TestCase):
    def test_check_accepts_a_valid_envelope(self) -> None:
        report = envelope_lint.check_payload(json.dumps(base_envelope()))
        self.assertTrue(report["valid"], report["findings"])

    def test_check_reports_non_json_stdout(self) -> None:
        report = envelope_lint.check_payload("phase: starting\n{}\n")
        self.assertFalse(report["valid"])
        self.assertIn("not a single JSON document", report["findings"][0])

    def test_check_reports_a_missing_layer_attribution(self) -> None:
        envelope = base_envelope()
        envelope["outcome"] = "failure"
        envelope["exit_code"] = 1
        report = envelope_lint.check_payload(json.dumps(envelope))
        self.assertFalse(report["valid"])
        self.assertTrue(
            any("layer attribution" in finding for finding in report["findings"]),
            report["findings"],
        )

    def test_static_scan_flags_a_non_conformant_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "legacy.py").write_text(
                "import json\n"
                "def main():\n"
                "    print('starting')\n"
                "    print(json.dumps({'status': 'failed'}))\n"
                "    return 1\n"
                "if __name__ == '__main__':\n"
                "    raise SystemExit(main())\n",
                encoding="utf-8",
            )
            report = envelope_lint.scan_tree(root)
            self.assertEqual(report["entry_points"], 1)
            self.assertEqual(report["conformant"], 0)
            self.assertEqual(report["stdout_purity_risk"], 1)
            result = report["results"][0]
            self.assertTrue(result["checks"]["stdout_json"])
            self.assertFalse(result["checks"]["envelope_library"])

    def test_static_scan_recognizes_a_conformant_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "modern.py").write_text(
                "from vaws_result_envelope import emit, make_failure, progress\n"
                "def main():\n"
                "    progress('start', 'working')\n"
                "    return emit({})\n"
                "if __name__ == '__main__':\n"
                "    raise SystemExit(main())\n",
                encoding="utf-8",
            )
            report = envelope_lint.scan_tree(root)
            self.assertEqual(report["conformant"], 1)
            self.assertEqual(report["conformance_rate"], 1.0)
            self.assertEqual(report["stdout_purity_risk"], 0)

    def test_test_modules_are_not_counted_as_entry_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "tests").mkdir()
            guard = "if __name__ == '__main__':\n    pass\n"
            (root / "scripts" / "test_thing.py").write_text(guard, encoding="utf-8")
            (root / "tests" / "runner.py").write_text(guard, encoding="utf-8")
            (root / "scripts" / "real.py").write_text(guard, encoding="utf-8")
            self.assertEqual(
                [path.name for path in envelope_lint.discover_entry_points(root)],
                ["real.py"],
            )

    def test_lint_itself_emits_one_valid_envelope(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "envelope_lint.py"),
                "scan",
                "--root",
                str(ROOT / ".agents" / "scripts"),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(ROOT),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        payload = json.loads(completed.stdout)
        validate_envelope(payload)
        self.assertNotIn("__VAWS_", completed.stdout)
        self.assertIn("__VAWS_PROGRESS__=", completed.stderr)
        self.assertGreater(payload["extensions"]["report"]["entry_points"], 0)

    def test_lint_run_mode_rejects_a_non_conforming_command(self) -> None:
        report = envelope_lint.run_and_check(
            [sys.executable, "-c", "print('progress: starting')"], timeout=30
        )
        self.assertFalse(report["valid"])
        self.assertTrue(report["findings"])

    def test_lint_run_mode_accepts_the_lint_itself(self) -> None:
        report = envelope_lint.run_and_check(
            [
                sys.executable,
                str(SCRIPTS / "envelope_lint.py"),
                "scan",
                "--root",
                str(ROOT / ".agents" / "hooks"),
            ],
            timeout=120,
        )
        self.assertTrue(report["valid"], report["findings"])
        self.assertEqual(report["exit_code"], 0)

    def test_lint_run_preserves_child_argv_separator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            recorded = directory / "actual-argv.json"
            envelope_path = directory / "child-envelope.json"
            child = directory / "child.py"
            envelope_path.write_text(
                json.dumps(base_envelope()) + "\n", encoding="utf-8"
            )
            child.write_text(
                "import json\n"
                "import pathlib\n"
                "import sys\n"
                f"pathlib.Path({str(recorded)!r}).write_text("
                "json.dumps(sys.argv[1:]), encoding='utf-8')\n"
                f"sys.stdout.write(pathlib.Path({str(envelope_path)!r})"
                ".read_text(encoding='utf-8'))\n"
                "raise SystemExit("
                "0 if sys.argv[1:] == ['--', '--literal'] else 67)\n",
                encoding="utf-8",
            )
            child_argv = [sys.executable, str(child), "--", "--literal"]
            direct = subprocess.run(
                child_argv,
                capture_output=True,
                text=True,
                check=False,
                cwd=str(ROOT),
            )
            self.assertEqual(direct.returncode, 0, direct.stderr[-2000:])
            self.assertEqual(
                json.loads(recorded.read_text(encoding="utf-8")),
                ["--", "--literal"],
            )
            wrapped = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "envelope_lint.py"),
                    "run",
                    "--",
                    *child_argv,
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(ROOT),
            )
            self.assertEqual(
                json.loads(recorded.read_text(encoding="utf-8")),
                ["--", "--literal"],
            )
            payload = json.loads(wrapped.stdout)
            validate_envelope(payload)
            self.assertEqual(wrapped.returncode, 0, wrapped.stderr[-2000:])
            report = payload["extensions"]["report"]
            self.assertTrue(report["valid"], report["findings"])
            self.assertEqual(report["command"], child_argv)
            self.assertEqual(report["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
