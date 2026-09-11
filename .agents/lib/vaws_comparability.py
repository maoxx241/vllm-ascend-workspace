#!/usr/bin/env python3
"""Observational comparability certificate for two-state experiments.

A pair of runs that differ in two ways cannot be attributed to either. This
module issues a certificate by diffing the identity recorded from each of two
actual runs, key by key. Every leaf is labelled ``observed``, ``declared``, or
``unknown``. A declaration is never promoted to an observation.

The certificate hangs off Run Manifest v1 as an artifact
(``comparability-certificate``). A comparison entry point must call
``consume_certificate`` before it is permitted to emit ``passed``. Consuming
recomputes the verdict from the identity body; a handwritten
``verdict: comparable`` is not accepted.

Empty ``workspace_snapshot`` / ``environment`` / ``model`` / ``topology`` are
recorded as ``unknown`` and block ``comparable`` (and therefore ``passed``).
Null and whitespace-only identity scalars are the same kind of unknown: they
do not satisfy must-observe and do not count as group presence. False, ``0``,
and domain-valid empty lists such as no extra serve/bench args remain evidence.
They are not rejected at run creation: planning before observation is
legitimate. Two empty objects comparing equal would be the worst form of
laundering, so emptiness is never treated as agreement.

Each certificate side retains declaration/observation mismatches so
``consume_certificate`` can recompute them. Winning leaves alone are not
enough: the observed value replaced the declared one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
KIND = "observational-comparability-certificate"
ORIGINS = frozenset({"observed", "declared", "unknown"})
IDENTITY_GROUPS = ("workspace_snapshot", "environment", "model", "topology")
# Prefixes that must have at least one observed leaf on each side. A declared
# value in these slots does not satisfy the requirement: that would launder a
# promise into a certificate.
DEFAULT_MUST_OBSERVE = IDENTITY_GROUPS
# Domain extras the audit named as identity. Callers pass the set they can
# actually record; absent prefixes become unknowns, not silent passes.
CORRECTNESS_MUST_OBSERVE = (
    *IDENTITY_GROUPS,
    "engine_args",
    "native_digest",
)
PERFORMANCE_MUST_OBSERVE = (
    *IDENTITY_GROUPS,
    "machine",
    "serve_args",
    "bench_args",
    "dataset",
    "max_concurrency",
    "request_rate",
    "npu_devices",
    "native_digest",
)
GRAPH_MUST_OBSERVE = (*IDENTITY_GROUPS, "native_digest")


class ComparabilityError(ValueError):
    """Raised when a pair is not comparable or a certificate cannot be used."""

    def __init__(self, message: str, *, certificate: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.certificate = dict(certificate) if certificate is not None else None


@dataclass(frozen=True)
class IdentityLeaf:
    value: str
    origin: str

    def __post_init__(self) -> None:
        if self.origin not in ORIGINS:
            raise ComparabilityError(
                f"identity origin must be one of {sorted(ORIGINS)}, "
                f"got {self.origin!r}"
            )


@dataclass(frozen=True)
class RunIdentity:
    """Flattened identity of one run, with an origin on every leaf."""

    run_id: str
    leaves: dict[str, IdentityLeaf]
    declaration_mismatches: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def value_map(self) -> dict[str, str]:
        return {key: leaf.value for key, leaf in self.leaves.items()}

    def origin_map(self) -> dict[str, str]:
        return {key: leaf.origin for key, leaf in self.leaves.items()}


def canonical_value(value: Any) -> str:
    """Canonical scalar used for key-by-key comparison."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        payload = list(value) if isinstance(value, tuple) else value
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return str(value)


def _is_unknown_identity_scalar(value: Any) -> bool:
    """Null and whitespace-only scalars are not observed evidence."""
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


def _has_identity_value(leaf: IdentityLeaf) -> bool:
    return bool(str(leaf.value).strip())


_MISMATCH_REQUIRED = ("key", "observed", "declared")
_MISMATCH_ALLOWED = frozenset(("key", "observed", "declared", "side"))


def _normalize_declaration_mismatches(
    raw: Any, *, source: str
) -> tuple[dict[str, str], ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ComparabilityError(f"{source} declaration_mismatches must be an array")
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        label = f"{source} declaration_mismatches[{index}]"
        if not isinstance(item, Mapping):
            raise ComparabilityError(f"{label} must be an object")
        extra = set(item) - _MISMATCH_ALLOWED
        if extra:
            raise ComparabilityError(
                f"{label} has unsupported fields: {', '.join(sorted(str(field) for field in extra))}"
            )
        missing = [field for field in _MISMATCH_REQUIRED if field not in item]
        if missing:
            raise ComparabilityError(f"{label} missing {', '.join(missing)}")
        key = item["key"]
        if not isinstance(key, str) or not key.strip():
            raise ComparabilityError(f"{label}.key must be a non-empty string")
        normalized.append(
            {
                "key": key,
                "observed": canonical_value(item["observed"]),
                "declared": canonical_value(item["declared"]),
            }
        )
    return tuple(normalized)


def flatten_mapping(prefix: str, value: Any) -> dict[str, str]:
    """Flatten a nested mapping into dotted scalar leaves.

    An empty mapping contributes no leaves. That is deliberate: ``{}`` must
    not compare equal to ``{}`` as if both sides observed the same nothing.
    Null and whitespace-only scalars are omitted for the same reason: they
    are unknown, not an observed empty string.
    """
    if isinstance(value, Mapping):
        if not value:
            return {}
        flat: dict[str, str] = {}
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            flat.update(flatten_mapping(child, value[key]))
        return flat
    if prefix == "":
        raise ComparabilityError("cannot flatten a non-mapping at the identity root")
    if _is_unknown_identity_scalar(value):
        return {}
    return {prefix: canonical_value(value)}


def _require_origin(origin: str) -> str:
    if origin not in ORIGINS:
        raise ComparabilityError(
            f"identity origin must be one of {sorted(ORIGINS)}, got {origin!r}"
        )
    return origin


def identity_from_leaves(
    run_id: str,
    leaves: Mapping[str, IdentityLeaf | Mapping[str, Any]],
    *,
    declaration_mismatches: Sequence[Mapping[str, str]] = (),
) -> RunIdentity:
    normalized: dict[str, IdentityLeaf] = {}
    for key, raw in leaves.items():
        if not isinstance(key, str) or not key.strip():
            raise ComparabilityError("identity keys must be non-empty strings")
        if isinstance(raw, IdentityLeaf):
            normalized[key] = raw
            continue
        if not isinstance(raw, Mapping) or "value" not in raw or "origin" not in raw:
            raise ComparabilityError(
                f"identity leaf {key!r} must be {{value, origin}}; the origin "
                "cannot be inferred, because that would launder a declaration"
            )
        normalized[key] = IdentityLeaf(
            value=canonical_value(raw["value"]),
            origin=_require_origin(str(raw["origin"])),
        )
    return RunIdentity(
        run_id=run_id,
        leaves=normalized,
        declaration_mismatches=_normalize_declaration_mismatches(
            declaration_mismatches, source="identity"
        ),
    )


def identity_from_mapping(
    run_id: str,
    mapping: Mapping[str, Any],
    *,
    origin: str,
    prefix: str = "",
) -> RunIdentity:
    """Label every leaf of ``mapping`` with an explicit origin.

    Callers choose the origin. This helper never upgrades ``declared`` to
    ``observed``.
    """
    _require_origin(origin)
    leaves = {
        key: IdentityLeaf(value=value, origin=origin)
        for key, value in flatten_mapping(prefix, mapping).items()
    }
    return RunIdentity(run_id=run_id, leaves=leaves)


def identity_from_manifest_fields(
    run_id: str,
    *,
    workspace_snapshot: Mapping[str, Any] | None = None,
    environment: Mapping[str, Any] | None = None,
    model: Mapping[str, Any] | None = None,
    topology: Mapping[str, Any] | None = None,
) -> RunIdentity:
    """Manifest identity is always declared: it was written at plan time."""
    groups = {
        "workspace_snapshot": workspace_snapshot or {},
        "environment": environment or {},
        "model": model or {},
        "topology": topology or {},
    }
    leaves: dict[str, IdentityLeaf] = {}
    for group, payload in groups.items():
        if not isinstance(payload, Mapping):
            raise ComparabilityError(f"{group} must be an object")
        leaves.update(identity_from_mapping(run_id, payload, origin="declared", prefix=group).leaves)
    return RunIdentity(run_id=run_id, leaves=leaves)


def identity_from_recorded_observation(
    run_id: str, observation: Mapping[str, Any]
) -> RunIdentity:
    """Treat a producer-recorded observation document as observed.

    The producer (harness, snapshot sidecar, measurement recorder) is trusted
    to pass only values it obtained from the run. This helper does not
    inspect how the document was produced; a hand-written observation is
    indistinguishable from a real one, and that limit belongs on the
    certificate's honest-limits list.
    """
    if not isinstance(observation, Mapping) or not observation:
        raise ComparabilityError("recorded observation must be a non-empty object")
    return identity_from_mapping(run_id, observation, origin="observed")


def identity_from_execution_block(
    execution: Mapping[str, Any],
    *,
    run_id: str,
    online: bool,
) -> RunIdentity:
    """Label a correctness ``execution`` block without laundering origins.

    Offline: ``engine_args`` and ``model`` were passed to the local ``LLM``
    constructor, so they are observed. Online: ``base_url`` and
    ``served_model`` were used for the HTTP request (observed);
    ``engine_args`` and ``model`` were never sent to the service (declared).
    Case files live in git; ``code.snapshot_commit`` already covers them.
    """
    if not isinstance(execution, Mapping):
        raise ComparabilityError("execution block must be an object")
    leaves: dict[str, IdentityLeaf] = {}
    engine_args = execution.get("engine_args", {})
    if engine_args is None:
        engine_args = {}
    if not isinstance(engine_args, Mapping):
        raise ComparabilityError("execution.engine_args must be an object")
    args_origin = "declared" if online else "observed"
    leaves.update(
        identity_from_mapping(
            run_id, engine_args, origin=args_origin, prefix="engine_args"
        ).leaves
    )
    field_origins = {
        "base_url": "observed",
        "served_model": "observed",
        "model": "declared" if online else "observed",
    }
    for field, origin in field_origins.items():
        if field not in execution:
            continue
        value = execution[field]
        if _is_unknown_identity_scalar(value):
            continue
        leaves[field] = IdentityLeaf(value=canonical_value(value), origin=origin)
    return RunIdentity(run_id=run_id, leaves=leaves)


def merge_identities(
    *identities: RunIdentity, run_id: str | None = None
) -> RunIdentity:
    """Merge identities. Observed wins; it is never replaced by declared.

    If the same key is both observed and declared with different values, the
    observed value is kept and the disagreement is recorded as a
    declaration/observation mismatch. Two disagreeing observations of the
    same key are an error: the producer contradicted itself.
    """
    if not identities:
        raise ComparabilityError("merge_identities requires at least one identity")
    merged_id = run_id if run_id is not None else identities[0].run_id
    by_key: dict[str, list[IdentityLeaf]] = {}
    mismatches: list[dict[str, str]] = []
    for identity in identities:
        mismatches.extend(identity.declaration_mismatches)
        for key, leaf in identity.leaves.items():
            by_key.setdefault(key, []).append(leaf)

    leaves: dict[str, IdentityLeaf] = {}
    for key, candidates in by_key.items():
        observed = [leaf for leaf in candidates if leaf.origin == "observed"]
        declared = [leaf for leaf in candidates if leaf.origin == "declared"]
        if observed:
            values = {leaf.value for leaf in observed}
            if len(values) != 1:
                raise ComparabilityError(
                    f"conflicting observations for {key!r}: {sorted(values)}"
                )
            chosen = observed[0]
            declared_values = {leaf.value for leaf in declared}
            if declared_values and chosen.value not in declared_values:
                mismatches.append(
                    {
                        "key": key,
                        "observed": chosen.value,
                        "declared": sorted(declared_values)[0],
                    }
                )
            leaves[key] = IdentityLeaf(value=chosen.value, origin="observed")
            continue
        if declared:
            values = {leaf.value for leaf in declared}
            if len(values) != 1:
                raise ComparabilityError(
                    f"conflicting declarations for {key!r}: {sorted(values)}"
                )
            leaves[key] = IdentityLeaf(value=declared[0].value, origin="declared")
            continue
        leaves[key] = IdentityLeaf(value=candidates[0].value, origin="unknown")
    return RunIdentity(
        run_id=merged_id,
        leaves=leaves,
        declaration_mismatches=tuple(mismatches),
    )


def _matches_prefix(key: str, prefix: str) -> bool:
    return key == prefix or key.startswith(prefix + ".")


def _group_of(key: str) -> str | None:
    for group in IDENTITY_GROUPS:
        if _matches_prefix(key, group):
            return group
    return None


def _side_payload(identity: RunIdentity) -> dict[str, Any]:
    mismatches = _normalize_declaration_mismatches(
        identity.declaration_mismatches, source=f"{identity.run_id} side"
    )
    return {
        "run_id": identity.run_id,
        "identity": {
            key: {"value": leaf.value, "origin": leaf.origin}
            for key, leaf in sorted(identity.leaves.items())
        },
        "declaration_mismatches": [dict(item) for item in mismatches],
    }


def identity_from_certificate_side(side: Mapping[str, Any]) -> RunIdentity:
    if not isinstance(side, Mapping):
        raise ComparabilityError("certificate side must be an object")
    run_id = side.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ComparabilityError("certificate side.run_id must be a non-empty string")
    raw_identity = side.get("identity")
    if not isinstance(raw_identity, Mapping):
        raise ComparabilityError("certificate side.identity must be an object")
    raw_mismatches = side.get("declaration_mismatches", ())
    return identity_from_leaves(
        run_id,
        raw_identity,
        declaration_mismatches=_normalize_declaration_mismatches(
            raw_mismatches, source="certificate side"
        ),
    )


def issue_certificate(
    baseline: RunIdentity,
    candidate: RunIdentity,
    *,
    vary: Iterable[str] = (),
    required_groups: Sequence[str] = IDENTITY_GROUPS,
    must_observe_prefixes: Sequence[str] = DEFAULT_MUST_OBSERVE,
) -> dict[str, Any]:
    """Diff two labelled identities and return a hard comparability verdict.

    Blocking reasons (any one is enough):

    * a required identity group has no leaves (empty ``{}`` → unknown);
    * a must-observe prefix has no observed leaf on a side (declared-only
      does not count);
    * a declaration/observation mismatch on either side;
    * an undeclared difference (confounder);
    * a declared varying key that is absent from both sides.
    """
    declared = list(dict.fromkeys(vary))
    left = baseline.leaves
    right = candidate.leaves
    keys = sorted(set(left) | set(right))

    def _row(key: str) -> dict[str, Any]:
        left_leaf = left.get(key)
        right_leaf = right.get(key)
        return {
            "key": key,
            "baseline": None if left_leaf is None else left_leaf.value,
            "candidate": None if right_leaf is None else right_leaf.value,
            "baseline_origin": None if left_leaf is None else left_leaf.origin,
            "candidate_origin": None if right_leaf is None else right_leaf.origin,
        }

    differing = [
        _row(key)
        for key in keys
        if (None if key not in left else left[key].value)
        != (None if key not in right else right[key].value)
    ]
    intended = [row for row in differing if row["key"] in declared]
    confounders = [row for row in differing if row["key"] not in declared]
    declared_but_identical = [
        key
        for key in declared
        if key in left
        and key in right
        and left[key].value == right[key].value
    ]
    declared_unknown = [key for key in declared if key not in keys]

    unknowns: list[dict[str, str]] = []
    declared_not_observed: list[dict[str, str]] = []
    for side_name, identity in (("baseline", baseline), ("candidate", candidate)):
        for key, leaf in identity.leaves.items():
            if not _has_identity_value(leaf):
                unknowns.append(
                    {
                        "key": key,
                        "side": side_name,
                        "reason": "empty-or-absent",
                    }
                )
    for group in required_groups:
        for side_name, identity in (("baseline", baseline), ("candidate", candidate)):
            leaves = [
                key
                for key, leaf in identity.leaves.items()
                if _matches_prefix(key, group) and _has_identity_value(leaf)
            ]
            if not leaves:
                if any(item["key"] == group and item["side"] == side_name for item in unknowns):
                    continue
                unknowns.append(
                    {
                        "key": group,
                        "side": side_name,
                        "reason": "empty-or-absent",
                    }
                )

    for prefix in must_observe_prefixes:
        for side_name, identity in (("baseline", baseline), ("candidate", candidate)):
            matching = [
                (key, leaf)
                for key, leaf in identity.leaves.items()
                if _matches_prefix(key, prefix) and _has_identity_value(leaf)
            ]
            if not matching:
                if any(item["key"] == prefix and item["side"] == side_name for item in unknowns):
                    continue
                unknowns.append(
                    {
                        "key": prefix,
                        "side": side_name,
                        "reason": "missing-must-observe",
                    }
                )
                continue
            if not any(leaf.origin == "observed" for _key, leaf in matching):
                declared_not_observed.append(
                    {
                        "key": prefix,
                        "side": side_name,
                        "origin": matching[0][1].origin,
                    }
                )

    baseline_mismatches = _normalize_declaration_mismatches(
        baseline.declaration_mismatches, source="baseline"
    )
    candidate_mismatches = _normalize_declaration_mismatches(
        candidate.declaration_mismatches, source="candidate"
    )
    mismatches = [
        {"side": "baseline", **item} for item in baseline_mismatches
    ] + [{"side": "candidate", **item} for item in candidate_mismatches]

    blocking: list[str] = []
    if unknowns:
        blocking.append(
            f"{len(unknowns)} required identity key(s) are unknown and "
            "cannot be treated as agreement"
        )
    if declared_not_observed:
        blocking.append(
            f"{len(declared_not_observed)} must-observe key(s) are only "
            "declared; a declaration is not an observation"
        )
    if mismatches:
        blocking.append(
            f"{len(mismatches)} declaration/observation mismatch(es) mean "
            "the written config is not what was recorded from the run"
        )
    if confounders:
        blocking.append(
            f"{len(confounders)} undeclared difference(s) can explain a "
            "delta on their own"
        )
    if declared_unknown:
        blocking.append(
            "declared varying key(s) absent from both runs: "
            + ", ".join(declared_unknown)
        )

    verdict = "comparable" if not blocking else "not-comparable"
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "verdict": verdict,
        "baseline": _side_payload(baseline),
        "candidate": _side_payload(candidate),
        "declared_varying": declared,
        "required_groups": list(required_groups),
        "must_observe_prefixes": list(must_observe_prefixes),
        "intended_differences": intended,
        "confounders": confounders,
        "unknowns": unknowns,
        "declared_not_observed": declared_not_observed,
        "declaration_mismatches": mismatches,
        "declared_but_identical": declared_but_identical,
        "declared_unknown": declared_unknown,
        "blocking_reasons": blocking,
        "interpretation": (
            "Any delta between these runs is attributable to the declared "
            "varying keys."
            if verdict == "comparable"
            else "A delta between these runs cannot be attributed to the "
            "declared change. Re-run with confounders held constant and "
            "record observations for every unknown or declared-only key."
        ),
    }


def consume_certificate(certificate: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute the verdict and refuse anything that is not comparable.

    Comparison entry points call this before emitting ``passed``. The
    stored ``verdict`` field is ignored: a certificate that launders a
    handwritten ``comparable`` would be worse than the declarative gate it
    replaces. Each side's ``declaration_mismatches`` is part of the identity
    body and is recomputed into the derived blocking lists.
    """
    if not isinstance(certificate, Mapping):
        raise ComparabilityError("certificate must be an object")
    if certificate.get("schema_version") != SCHEMA_VERSION:
        raise ComparabilityError(
            f"certificate schema_version must be {SCHEMA_VERSION}"
        )
    if certificate.get("kind") != KIND:
        raise ComparabilityError(f"certificate kind must be {KIND!r}")
    baseline = identity_from_certificate_side(certificate.get("baseline", {}))
    candidate = identity_from_certificate_side(certificate.get("candidate", {}))
    vary = certificate.get("declared_varying", [])
    if not isinstance(vary, list) or any(not isinstance(item, str) for item in vary):
        raise ComparabilityError("declared_varying must be an array of strings")
    required = certificate.get("required_groups", list(IDENTITY_GROUPS))
    must_observe = certificate.get("must_observe_prefixes", list(DEFAULT_MUST_OBSERVE))
    if not isinstance(required, list) or not isinstance(must_observe, list):
        raise ComparabilityError(
            "required_groups and must_observe_prefixes must be arrays"
        )
    rebuilt = issue_certificate(
        baseline,
        candidate,
        vary=vary,
        required_groups=[str(item) for item in required],
        must_observe_prefixes=[str(item) for item in must_observe],
    )
    if rebuilt["verdict"] != "comparable":
        raise ComparabilityError(
            "comparability certificate is not-comparable: "
            + "; ".join(rebuilt["blocking_reasons"]),
            certificate=rebuilt,
        )
    return rebuilt


def require_comparable(certificate: Mapping[str, Any]) -> dict[str, Any]:
    """Alias for :func:`consume_certificate`."""
    return consume_certificate(certificate)


def format_undeclared_fields(certificate: Mapping[str, Any]) -> str:
    """Stable clause used by correctness when confounders are execution keys."""
    keys = [row["key"] for row in certificate.get("confounders", [])]
    if not keys:
        return ""
    return "undeclared fields: " + ", ".join(keys)
