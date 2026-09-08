#!/usr/bin/env python3
"""Offline tests for the observational comparability certificate.

The audit §8.2 six-case hardware matrix is encoded here as recorded
observations. Those cases have not been run on Ascend hardware.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_comparability import (  # noqa: E402
    CORRECTNESS_MUST_OBSERVE,
    ComparabilityError,
    IDENTITY_GROUPS,
    IdentityLeaf,
    KIND,
    PERFORMANCE_MUST_OBSERVE,
    RunIdentity,
    consume_certificate,
    identity_from_certificate_side,
    identity_from_execution_block,
    identity_from_manifest_fields,
    identity_from_recorded_observation,
    issue_certificate,
    merge_identities,
)


def observed(
    run_id: str = "run-a",
    *,
    commit: str = "aaaa1111",
    container: str = "c-1",
    tp: int = 2,
    dp: int = 1,
    enforce_eager: bool = False,
    max_concurrency: int = 8,
    native_digest: str = "cc" * 32,
    extra: dict | None = None,
) -> RunIdentity:
    payload = {
        "workspace_snapshot": {
            "vllm_ascend_commit": commit,
            "dirty": False,
        },
        "environment": {
            "cann": "8.3",
            "torch_npu": "2.1",
            "container": container,
        },
        "model": {
            "path": "/models/example",
            "weight_hash": "ab" * 32,
        },
        "topology": {"tp": tp, "dp": dp, "npu_devices": [0, 1]},
        "engine_args": {
            "tensor_parallel_size": tp,
            "enforce_eager": enforce_eager,
        },
        "native_digest": native_digest,
        "serve_args": ["--host", "service.example.invalid"],
        "bench_args": ["--num-prompts", "64"],
        "dataset": "sharegpt",
        "max_concurrency": max_concurrency,
        "request_rate": "inf",
        "npu_devices": [0, 1],
    }
    if extra:
        payload.update(extra)
    return identity_from_recorded_observation(run_id, payload)


def issue(
    baseline: RunIdentity,
    candidate: RunIdentity,
    *,
    vary: list[str] | None = None,
    must_observe: tuple[str, ...] = PERFORMANCE_MUST_OBSERVE,
) -> dict:
    return issue_certificate(
        baseline,
        candidate,
        vary=vary or [],
        must_observe_prefixes=must_observe,
    )


class OriginLabelTests(unittest.TestCase):
    def test_manifest_fields_are_declared_never_observed(self) -> None:
        identity = identity_from_manifest_fields(
            "run-a",
            workspace_snapshot={"vllm_ascend_commit": "aaaa1111"},
            environment={"cann": "8.3"},
            model={"path": "/models/example"},
            topology={"tp": 2},
        )
        self.assertTrue(identity.leaves)
        self.assertTrue(
            all(leaf.origin == "declared" for leaf in identity.leaves.values())
        )
        certificate = issue(
            identity,
            identity,
            must_observe=IDENTITY_GROUPS,
        )
        self.assertEqual(certificate["verdict"], "not-comparable")
        self.assertTrue(certificate["declared_not_observed"])

    def test_empty_manifest_groups_are_unknown_not_agreement(self) -> None:
        empty = identity_from_manifest_fields("run-a")
        self.assertEqual(empty.leaves, {})
        certificate = issue(empty, empty, must_observe=IDENTITY_GROUPS)
        self.assertEqual(certificate["verdict"], "not-comparable")
        unknown_keys = {item["key"] for item in certificate["unknowns"]}
        self.assertEqual(unknown_keys, set(IDENTITY_GROUPS))
        self.assertTrue(
            any("unknown" in reason for reason in certificate["blocking_reasons"])
        )

    def test_online_execution_does_not_label_engine_args_observed(self) -> None:
        identity = identity_from_execution_block(
            {
                "engine_args": {"enforce_eager": True, "tensor_parallel_size": 2},
                "model": "/models/example",
                "base_url": "http://service.example.invalid:8000",
                "served_model": "example",
            },
            run_id="run-a",
            online=True,
        )
        self.assertEqual(identity.leaves["engine_args.enforce_eager"].origin, "declared")
        self.assertEqual(identity.leaves["model"].origin, "declared")
        self.assertEqual(identity.leaves["base_url"].origin, "observed")
        self.assertEqual(identity.leaves["served_model"].origin, "observed")
        self.assertNotIn("cases_sha256", identity.leaves)

    def test_offline_execution_labels_constructor_args_observed(self) -> None:
        identity = identity_from_execution_block(
            {
                "engine_args": {"enforce_eager": False, "tensor_parallel_size": 2},
                "model": "/models/example",
                "base_url": None,
                "served_model": None,
            },
            run_id="run-a",
            online=False,
        )
        self.assertEqual(identity.leaves["engine_args.enforce_eager"].origin, "observed")
        self.assertEqual(identity.leaves["model"].origin, "observed")

    def test_merge_does_not_promote_declared_over_observed(self) -> None:
        declared = identity_from_manifest_fields(
            "run-a",
            workspace_snapshot={"vllm_ascend_commit": "declared-only"},
            environment={"cann": "8.3"},
            model={"path": "/models/example"},
            topology={"tp": 8},
        )
        recorded = identity_from_recorded_observation(
            "run-a",
            {
                "workspace_snapshot": {"vllm_ascend_commit": "aaaa1111"},
                "environment": {"cann": "8.3"},
                "model": {"path": "/models/example"},
                "topology": {"tp": 2},
            },
        )
        merged = merge_identities(declared, recorded)
        self.assertEqual(
            merged.leaves["workspace_snapshot.vllm_ascend_commit"].origin,
            "observed",
        )
        self.assertEqual(
            merged.leaves["workspace_snapshot.vllm_ascend_commit"].value,
            "aaaa1111",
        )
        self.assertEqual(merged.leaves["topology.tp"].value, "2")
        self.assertTrue(merged.declaration_mismatches)

    def test_leaf_without_origin_is_rejected(self) -> None:
        from vaws_comparability import identity_from_leaves

        with self.assertRaisesRegex(ComparabilityError, "cannot be inferred"):
            identity_from_leaves("run-a", {"model.path": "only-a-value"})


class VerdictTests(unittest.TestCase):
    def test_single_declared_difference_is_comparable(self) -> None:
        certificate = issue(
            observed("base"),
            observed("cand", commit="bbbb2222"),
            vary=["workspace_snapshot.vllm_ascend_commit"],
        )
        self.assertEqual(certificate["verdict"], "comparable")
        self.assertEqual(certificate["confounders"], [])
        self.assertEqual(len(certificate["intended_differences"]), 1)
        consume_certificate(certificate)

    def test_one_undeclared_difference_is_a_hard_not_comparable(self) -> None:
        certificate = issue(
            observed("base"),
            observed("cand", commit="bbbb2222", container="c-2"),
            vary=["workspace_snapshot.vllm_ascend_commit"],
        )
        self.assertEqual(certificate["verdict"], "not-comparable")
        self.assertEqual(
            [row["key"] for row in certificate["confounders"]],
            ["environment.container"],
        )
        with self.assertRaisesRegex(ComparabilityError, "undeclared difference"):
            consume_certificate(certificate)

    def test_handwritten_comparable_verdict_is_not_consumed(self) -> None:
        certificate = issue(
            observed("base"),
            observed("cand", commit="bbbb2222", tp=8),
            vary=["workspace_snapshot.vllm_ascend_commit"],
        )
        self.assertEqual(certificate["verdict"], "not-comparable")
        forged = copy.deepcopy(certificate)
        forged["verdict"] = "comparable"
        forged["blocking_reasons"] = []
        forged["confounders"] = []
        with self.assertRaisesRegex(ComparabilityError, "not-comparable"):
            consume_certificate(forged)

    def test_declaration_observation_mismatch_blocks(self) -> None:
        declared = identity_from_manifest_fields(
            "run-a",
            workspace_snapshot={"vllm_ascend_commit": "aaaa1111"},
            environment={"cann": "8.3"},
            model={"path": "/models/example"},
            topology={"tp": 8},
        )
        recorded = observed("run-a")
        merged = merge_identities(declared, recorded)
        other = observed("run-b")
        certificate = issue(merged, other, vary=[])
        self.assertEqual(certificate["verdict"], "not-comparable")
        self.assertTrue(certificate["declaration_mismatches"])


class HardwareMatrixAcceptanceTests(unittest.TestCase):
    """Audit §8.2 six-case matrix, as recorded observations. Not run on NPU."""

    must_observe = PERFORMANCE_MUST_OBSERVE

    def test_case_1_same_code_same_config_is_comparable(self) -> None:
        certificate = issue(observed("base"), observed("cand"), vary=[])
        self.assertEqual(certificate["verdict"], "comparable")

    def test_case_2_only_candidate_code_is_comparable(self) -> None:
        certificate = issue(
            observed("base"),
            observed("cand", commit="bbbb2222", native_digest="dd" * 32),
            vary=[
                "workspace_snapshot.vllm_ascend_commit",
                "native_digest",
            ],
        )
        self.assertEqual(certificate["verdict"], "comparable")
        keys = {row["key"] for row in certificate["intended_differences"]}
        self.assertIn("workspace_snapshot.vllm_ascend_commit", keys)

    def test_case_3_extra_enforce_eager_is_not_comparable(self) -> None:
        certificate = issue(
            observed("base"),
            observed("cand", commit="bbbb2222", enforce_eager=True),
            vary=["workspace_snapshot.vllm_ascend_commit"],
            must_observe=CORRECTNESS_MUST_OBSERVE,
        )
        self.assertEqual(certificate["verdict"], "not-comparable")
        self.assertIn(
            "engine_args.enforce_eager",
            [row["key"] for row in certificate["confounders"]],
        )

    def test_case_4_candidate_tensor_parallel_change_is_not_comparable(self) -> None:
        certificate = issue(
            observed("base"),
            observed("cand", commit="bbbb2222", tp=8),
            vary=["workspace_snapshot.vllm_ascend_commit"],
        )
        self.assertEqual(certificate["verdict"], "not-comparable")
        confounder_keys = [row["key"] for row in certificate["confounders"]]
        self.assertIn("topology.tp", confounder_keys)
        self.assertIn("engine_args.tensor_parallel_size", confounder_keys)

    def test_case_5_dp_greater_than_one_with_extra_flag_is_not_comparable(self) -> None:
        certificate = issue(
            observed("base", dp=2),
            observed("cand", dp=2, commit="bbbb2222", enforce_eager=True),
            vary=["workspace_snapshot.vllm_ascend_commit"],
            must_observe=CORRECTNESS_MUST_OBSERVE,
        )
        self.assertEqual(certificate["verdict"], "not-comparable")
        self.assertIn(
            "engine_args.enforce_eager",
            [row["key"] for row in certificate["confounders"]],
        )

    def test_case_6_different_max_concurrency_is_not_comparable(self) -> None:
        certificate = issue(
            observed("base"),
            observed("cand", max_concurrency=32),
            vary=["workspace_snapshot.vllm_ascend_commit"],
        )
        self.assertEqual(certificate["verdict"], "not-comparable")
        self.assertEqual(
            [row["key"] for row in certificate["confounders"]],
            ["max_concurrency"],
        )


class CertificateShapeTests(unittest.TestCase):
    def test_every_leaf_carries_origin(self) -> None:
        certificate = issue(observed("base"), observed("cand"))
        self.assertEqual(certificate["kind"], KIND)
        for side in ("baseline", "candidate"):
            for key, leaf in certificate[side]["identity"].items():
                self.assertIn(leaf["origin"], {"observed", "declared", "unknown"}, key)
                self.assertIn("value", leaf)


def _mismatched_pair() -> tuple[RunIdentity, RunIdentity]:
    declared = identity_from_manifest_fields(
        "run-a",
        workspace_snapshot={"vllm_ascend_commit": "aaaa1111"},
        environment={"cann": "8.3"},
        model={"path": "/models/example"},
        topology={"tp": 8},
    )
    baseline = merge_identities(declared, observed("run-a"))
    candidate = merge_identities(
        identity_from_manifest_fields(
            "run-b",
            workspace_snapshot={"vllm_ascend_commit": "aaaa1111"},
            environment={"cann": "8.3"},
            model={"path": "/models/example"},
            topology={"tp": 8},
        ),
        observed("run-b"),
    )
    return baseline, candidate


class MismatchPersistenceTests(unittest.TestCase):
    def test_issue_serialize_consume_round_trip_keeps_mismatch_blocking(self) -> None:
        baseline, candidate = _mismatched_pair()
        certificate = issue(baseline, candidate, vary=[])
        self.assertEqual(certificate["verdict"], "not-comparable")
        self.assertTrue(certificate["declaration_mismatches"])
        for side in ("baseline", "candidate"):
            self.assertTrue(certificate[side].get("declaration_mismatches"))
        restored = json.loads(json.dumps(certificate))
        rebuilt_baseline = identity_from_certificate_side(restored["baseline"])
        self.assertTrue(rebuilt_baseline.declaration_mismatches)
        with self.assertRaisesRegex(ComparabilityError, "declaration/observation mismatch") as raised:
            consume_certificate(restored)
        blocked = raised.exception.certificate
        self.assertIsNotNone(blocked)
        assert blocked is not None
        self.assertEqual(blocked["verdict"], "not-comparable")
        self.assertTrue(blocked["declaration_mismatches"])

    def test_comparable_control_survives_json_round_trip(self) -> None:
        certificate = issue(
            observed("base"),
            observed("cand", commit="bbbb2222"),
            vary=["workspace_snapshot.vllm_ascend_commit"],
        )
        self.assertEqual(certificate["verdict"], "comparable")
        consume_certificate(json.loads(json.dumps(certificate)))

    def test_malformed_mismatch_record_is_rejected(self) -> None:
        certificate = issue(observed("base"), observed("cand"))
        certificate["baseline"]["declaration_mismatches"] = [
            {"key": "topology.tp", "observed": "2"}
        ]
        with self.assertRaisesRegex(ComparabilityError, "declaration_mismatches"):
            consume_certificate(certificate)

    def test_non_object_mismatch_record_is_rejected(self) -> None:
        certificate = issue(observed("base"), observed("cand"))
        certificate["candidate"]["declaration_mismatches"] = ["topology.tp"]
        with self.assertRaisesRegex(ComparabilityError, "declaration_mismatches"):
            consume_certificate(certificate)


class UnknownIdentityValueTests(unittest.TestCase):
    def test_null_and_blank_required_identity_is_unknown(self) -> None:
        payload = {
            "workspace_snapshot": {"vllm_ascend_commit": None},
            "environment": {"cann": "   "},
            "model": {"weight_hash": ""},
            "topology": {"tp": None},
            "native_digest": None,
        }
        identity = identity_from_recorded_observation("run-a", payload)
        certificate = issue(
            identity,
            identity,
            must_observe=CORRECTNESS_MUST_OBSERVE,
        )
        self.assertEqual(certificate["verdict"], "not-comparable")
        unknown_fields = {(item["side"], item["key"]) for item in certificate["unknowns"]}
        self.assertIn(("baseline", "workspace_snapshot"), unknown_fields)
        self.assertIn(("candidate", "workspace_snapshot"), unknown_fields)
        self.assertIn(("baseline", "environment"), unknown_fields)
        self.assertIn(("baseline", "model"), unknown_fields)
        self.assertIn(("baseline", "topology"), unknown_fields)
        self.assertIn(("baseline", "native_digest"), unknown_fields)
        with self.assertRaisesRegex(ComparabilityError, "unknown"):
            consume_certificate(certificate)

    def test_blank_native_digest_does_not_satisfy_must_observe(self) -> None:
        identity = observed("run-a", native_digest="  ")
        certificate = issue(identity, identity, must_observe=CORRECTNESS_MUST_OBSERVE)
        self.assertEqual(certificate["verdict"], "not-comparable")
        unknown_fields = {(item["side"], item["key"]) for item in certificate["unknowns"]}
        self.assertIn(("baseline", "native_digest"), unknown_fields)
        self.assertIn(("candidate", "native_digest"), unknown_fields)

    def test_blank_observed_leaf_does_not_count_as_evidence(self) -> None:
        leaves = dict(observed("run-a").leaves)
        leaves["native_digest"] = IdentityLeaf(value="", origin="observed")
        identity = RunIdentity(run_id="run-a", leaves=leaves)
        certificate = issue(identity, identity, must_observe=CORRECTNESS_MUST_OBSERVE)
        self.assertEqual(certificate["verdict"], "not-comparable")
        unknown_fields = {(item["side"], item["key"]) for item in certificate["unknowns"]}
        self.assertIn(("baseline", "native_digest"), unknown_fields)

    def test_false_zero_and_empty_args_remain_observed_evidence(self) -> None:
        payload = {
            "workspace_snapshot": {"vllm_ascend_commit": "aaaa1111", "dirty": False},
            "environment": {"cann": "8.3"},
            "model": {"path": "/models/example"},
            "topology": {"tp": 0, "dp": 1},
            "native_digest": "cc" * 32,
            "serve_args": [],
            "bench_args": [],
            "max_concurrency": 0,
        }
        identity = identity_from_recorded_observation("run-a", payload)
        self.assertEqual(identity.leaves["workspace_snapshot.dirty"].value, "false")
        self.assertEqual(identity.leaves["topology.tp"].value, "0")
        self.assertEqual(identity.leaves["max_concurrency"].value, "0")
        self.assertEqual(identity.leaves["serve_args"].value, "[]")
        self.assertEqual(identity.leaves["bench_args"].value, "[]")
        certificate = issue(
            identity,
            identity,
            must_observe=(
                "workspace_snapshot",
                "environment",
                "model",
                "topology",
                "native_digest",
                "serve_args",
                "bench_args",
                "max_concurrency",
            ),
        )
        self.assertEqual(certificate["verdict"], "comparable")
        consume_certificate(certificate)


if __name__ == "__main__":
    unittest.main()
