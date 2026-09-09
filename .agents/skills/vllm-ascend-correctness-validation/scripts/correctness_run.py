#!/usr/bin/env python3
"""Create and compare normalized vLLM Ascend correctness runs."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)


from vaws_comparability import (  # noqa: E402
    CORRECTNESS_MUST_OBSERVE,
    ComparabilityError,
    consume_certificate,
    identity_from_execution_block,
    identity_from_manifest_fields,
    identity_from_recorded_observation,
    issue_certificate,
    merge_identities,
)
from vaws_coordinator.code_identity import manifest_code  # noqa: E402
from vaws_coordinator.run_manifest import (  # noqa: E402
    RunManifestError,
    add_artifact,
    load_manifest,
    new_manifest,
    transition_status,
    write_manifest,
)

SCHEMA_VERSION = 1
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
# Keys of the result `execution` block that are flattened one level (dotted).
EXECUTION_NESTED_FIELDS = ("engine_args",)
CASE_MODES = frozenset({"offline-generate", "offline-chat", "online-chat", "aisbench"})
RESULT_STATUSES = frozenset({"ok", "error", "unsupported"})
PASS_CLASSES = frozenset({"exact_match", "numerical_difference_within_tolerance"})
FAIL_CLASSES = frozenset(
    {"token_divergence", "numerical_regression", "task_metric_regression"}
)
INCONCLUSIVE_CLASSES = frozenset(
    {"flaky_or_nondeterministic", "infrastructure_failure", "unsupported_combination"}
)
CLASS_PRIORITY = {
    "infrastructure_failure": 0,
    "unsupported_combination": 1,
    "flaky_or_nondeterministic": 2,
    "task_metric_regression": 3,
    "token_divergence": 4,
    "numerical_regression": 5,
    "numerical_difference_within_tolerance": 6,
    "exact_match": 7,
}


class CorrectnessError(ValueError):
    """Raised when correctness input or state is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def emit_progress(phase: str, **details: Any) -> None:
    print(json.dumps({"phase": phase, **details}, ensure_ascii=False), file=sys.stderr)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorrectnessError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CorrectnessError(f"{label} root must be an object")
    return payload


def validate_cases_document(document: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if document.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty array")
        cases = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        prefix = f"cases[{index}]"
        if not isinstance(case, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not SAFE_ID_RE.fullmatch(case_id):
            errors.append(f"{prefix}.id must be a lowercase safe identifier")
        elif case_id in seen:
            errors.append(f"{prefix}.id is duplicated: {case_id}")
        else:
            seen.add(case_id)
        mode = case.get("mode")
        if mode not in CASE_MODES:
            errors.append(f"{prefix}.mode must be one of: {', '.join(sorted(CASE_MODES))}")
        repeats = case.get("repeats", 1)
        if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1:
            errors.append(f"{prefix}.repeats must be a positive integer")
        sampling = case.get("sampling", {})
        if not isinstance(sampling, Mapping):
            errors.append(f"{prefix}.sampling must be an object")
        elif mode != "aisbench":
            if sampling.get("temperature", 0) != 0:
                errors.append(f"{prefix}.sampling.temperature must be 0 for exact comparison")
            if "seed" not in sampling:
                errors.append(f"{prefix}.sampling.seed is required")
        request = case.get("request")
        if mode != "aisbench" and not isinstance(request, Mapping):
            errors.append(f"{prefix}.request must be an object")
        comparison = case.get("comparison", {})
        if not isinstance(comparison, Mapping):
            errors.append(f"{prefix}.comparison must be an object")
    if errors:
        raise CorrectnessError("; ".join(errors))


def validate_result_document(document: Mapping[str, Any], *, label: str) -> None:
    errors: list[str] = []
    if document.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{label}.schema_version must be {SCHEMA_VERSION}")
    cases = document.get("cases")
    if not isinstance(cases, list):
        errors.append(f"{label}.cases must be an array")
        cases = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        prefix = f"{label}.cases[{index}]"
        if not isinstance(case, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not SAFE_ID_RE.fullmatch(case_id):
            errors.append(f"{prefix}.id must be a lowercase safe identifier")
        elif case_id in seen:
            errors.append(f"{prefix}.id is duplicated")
        else:
            seen.add(case_id)
        if case.get("status") not in RESULT_STATUSES:
            errors.append(
                f"{prefix}.status must be one of: {', '.join(sorted(RESULT_STATUSES))}"
            )
        outputs = case.get("outputs", [])
        if not isinstance(outputs, list):
            errors.append(f"{prefix}.outputs must be an array")
        if not isinstance(case.get("metrics", {}), Mapping):
            errors.append(f"{prefix}.metrics must be an object")
    if errors:
        raise CorrectnessError("; ".join(errors))


def init_run(
    run_dir: Path,
    *,
    run_id: str,
    cases_path: Path,
    baseline_label: str,
    candidate_label: str,
    workspace_snapshot: Mapping[str, Any] | None = None,
    environment: Mapping[str, Any] | None = None,
    model: Mapping[str, Any] | None = None,
    topology: Mapping[str, Any] | None = None,
    baseline_command: Sequence[str] | None = None,
    candidate_command: Sequence[str] | None = None,
    allowed_differences: Sequence[str] | None = None,
    parent_run_id: str | None = None,
    created_at: str | None = None,
    code: Mapping[str, Any] | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    if run_dir.exists() and any(run_dir.iterdir()):
        raise CorrectnessError(f"run directory is not empty: {run_dir}")
    if baseline_label == candidate_label:
        raise CorrectnessError("baseline-label and candidate-label must differ")
    declared = sorted(set(allowed_differences or []))
    for key in declared:
        if not key.strip():
            raise CorrectnessError("allowed-difference keys must be non-empty")
    cases_document = _load_json(cases_path, "cases file")
    validate_cases_document(cases_document)
    timestamp = created_at or utc_now()
    run_dir.mkdir(parents=True, exist_ok=True)
    copied_cases = run_dir / "cases.json"
    _atomic_write_json(copied_cases, cases_document)
    _atomic_write_json(run_dir / "environment.json", dict(environment or {}))
    run_state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "baseline_label": baseline_label,
        "candidate_label": candidate_label,
        "baseline_command": list(baseline_command or []),
        "candidate_command": list(candidate_command or []),
        "allowed_differences": declared,
        "status": "planned",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _atomic_write_json(run_dir / "run.json", run_state)
    reproduction = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"# Baseline: {baseline_label}\n"
        f"{shlex.join(baseline_command) if baseline_command else '# command not recorded'}\n\n"
        f"# Candidate: {candidate_label}\n"
        f"{shlex.join(candidate_command) if candidate_command else '# command not recorded'}\n"
    )
    _atomic_write_text(run_dir / "reproduction.sh", reproduction)
    manifest = new_manifest(
        run_type="correctness",
        run_id=run_id,
        parent_run_id=parent_run_id,
        code=code,
        workspace_root=workspace_root or ROOT,
        workspace_snapshot=workspace_snapshot,
        environment=environment,
        model=model,
        topology=topology,
        command=list(candidate_command or []),
        created_at=timestamp,
    )
    for name, kind, uri in (
        ("cases", "validation-cases", "cases.json"),
        ("environment", "environment", "environment.json"),
        ("reproduction", "reproduction", "reproduction.sh"),
    ):
        manifest = add_artifact(
            manifest, name=name, kind=kind, uri=uri, updated_at=timestamp
        )
    write_manifest(run_dir / "manifest.json", manifest)
    return run_state


def _flatten_numbers(value: Any, *, label: str) -> list[float]:
    if isinstance(value, bool):
        raise CorrectnessError(f"{label} contains a boolean")
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, list):
        result: list[float] = []
        for index, item in enumerate(value):
            result.extend(_flatten_numbers(item, label=f"{label}[{index}]"))
        return result
    raise CorrectnessError(f"{label} must contain numbers or nested arrays")


def _close(left: float, right: float, *, atol: float, rtol: float) -> bool:
    if math.isnan(left) or math.isnan(right):
        return math.isnan(left) and math.isnan(right)
    if math.isinf(left) or math.isinf(right):
        return left == right
    return math.isclose(left, right, abs_tol=atol, rel_tol=rtol)


def _output_signature(output: Mapping[str, Any]) -> tuple[str, Any] | None:
    for field in ("token_ids", "tokens", "text"):
        if field in output:
            value = output[field]
            if isinstance(value, list):
                return field, tuple(value)
            if isinstance(value, str):
                return field, value
    return None


def _is_vacuous_signature(signature: tuple[str, Any]) -> bool:
    """Two empty answers are not agreement; they are missing evidence."""
    field, value = signature
    if field == "text":
        return not str(value).strip()
    if field in {"token_ids", "tokens"}:
        return len(value) == 0
    return False


def _is_stable(outputs: list[Any]) -> bool:
    if len(outputs) <= 1:
        return True
    signatures: list[tuple[str, Any] | None] = []
    for output in outputs:
        if not isinstance(output, Mapping):
            return False
        signatures.append(_output_signature(output))
    return signatures[0] is not None and all(
        signature == signatures[0] for signature in signatures[1:]
    )


def _compare_metrics(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    rules: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    regressions: list[dict[str, Any]] = []
    changed = False
    for name, raw_rule in sorted(rules.items()):
        if not isinstance(raw_rule, Mapping):
            raise CorrectnessError(f"metric rule {name!r} must be an object")
        if name not in baseline or name not in candidate:
            regressions.append({"metric": name, "reason": "missing"})
            continue
        left = float(baseline[name])
        right = float(candidate[name])
        changed = changed or left != right
        direction = raw_rule.get("direction", "higher")
        if direction not in {"higher", "lower"}:
            raise CorrectnessError(f"metric rule {name!r} has invalid direction")
        degradation = left - right if direction == "higher" else right - left
        relative = degradation / abs(left) if left != 0 else (
            math.inf if degradation > 0 else 0.0
        )
        max_absolute = float(raw_rule.get("max_absolute_regression", 0.0))
        max_relative = float(raw_rule.get("max_relative_regression", 0.0))
        if degradation > max_absolute or relative > max_relative:
            regressions.append(
                {
                    "metric": name,
                    "baseline": left,
                    "candidate": right,
                    "absolute_regression": degradation,
                    "relative_regression": relative,
                    "allowed_absolute": max_absolute,
                    "allowed_relative": max_relative,
                }
            )
    return regressions, changed


def compare_case(
    case_config: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    candidate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    case_id = case_config["id"]
    if baseline is None or candidate is None:
        return {
            "id": case_id,
            "classification": "infrastructure_failure",
            "details": {"reason": "case result missing from baseline or candidate"},
        }
    statuses = {baseline.get("status"), candidate.get("status")}
    if "error" in statuses:
        return {
            "id": case_id,
            "classification": "infrastructure_failure",
            "details": {
                "baseline_error": baseline.get("error"),
                "candidate_error": candidate.get("error"),
            },
        }
    if "unsupported" in statuses:
        return {
            "id": case_id,
            "classification": "unsupported_combination",
            "details": {
                "baseline_status": baseline.get("status"),
                "candidate_status": candidate.get("status"),
            },
        }
    comparison_config = case_config.get("comparison", {})
    metric_rules = comparison_config.get("metric_rules", {})
    metric_regressions, metrics_changed = _compare_metrics(
        baseline.get("metrics", {}),
        candidate.get("metrics", {}),
        metric_rules,
    )
    if metric_regressions:
        return {
            "id": case_id,
            "classification": "task_metric_regression",
            "details": {"metric_regressions": metric_regressions},
        }
    if case_config.get("mode") == "aisbench":
        return {
            "id": case_id,
            "classification": (
                "numerical_difference_within_tolerance"
                if metrics_changed
                else "exact_match"
            ),
            "details": {
                "evidence": "task-metrics",
                "baseline_metrics": baseline.get("metrics", {}),
                "candidate_metrics": candidate.get("metrics", {}),
            },
        }
    baseline_outputs = baseline.get("outputs", [])
    candidate_outputs = candidate.get("outputs", [])
    if not _is_stable(baseline_outputs) or not _is_stable(candidate_outputs):
        return {
            "id": case_id,
            "classification": "flaky_or_nondeterministic",
            "details": {
                "baseline_repeats": len(baseline_outputs),
                "candidate_repeats": len(candidate_outputs),
            },
        }
    if not baseline_outputs or not candidate_outputs:
        return {
            "id": case_id,
            "classification": "infrastructure_failure",
            "details": {"reason": "no normalized outputs"},
        }

    left_output = baseline_outputs[0]
    right_output = candidate_outputs[0]
    left_signature = _output_signature(left_output)
    right_signature = _output_signature(right_output)
    if left_signature is None or right_signature is None:
        return {
            "id": case_id,
            "classification": "infrastructure_failure",
            "details": {"reason": "no comparable token, token-id, or text field"},
        }
    if left_signature != right_signature:
        return {
            "id": case_id,
            "classification": "token_divergence",
            "details": {
                "baseline_signature_type": left_signature[0],
                "candidate_signature_type": right_signature[0],
                "baseline": left_signature[1],
                "candidate": right_signature[1],
            },
        }
    if _is_vacuous_signature(left_signature):
        return {
            "id": case_id,
            "classification": "infrastructure_failure",
            "details": {
                "reason": "empty-output-is-not-agreement",
                "signature_type": left_signature[0],
            },
        }

    left_numeric = left_output.get("numerics", {})
    right_numeric = right_output.get("numerics", {})
    if not isinstance(left_numeric, Mapping) or not isinstance(right_numeric, Mapping):
        raise CorrectnessError(f"{case_id}: numerics must be objects")
    atol = float(comparison_config.get("atol", 0.0))
    rtol = float(comparison_config.get("rtol", 0.0))
    numeric_differences: list[dict[str, Any]] = []
    numeric_within_tolerance = False
    for name in sorted(set(left_numeric) | set(right_numeric)):
        if name not in left_numeric or name not in right_numeric:
            numeric_differences.append({"field": name, "reason": "missing"})
            continue
        left_values = _flatten_numbers(left_numeric[name], label=f"baseline.{name}")
        right_values = _flatten_numbers(right_numeric[name], label=f"candidate.{name}")
        if len(left_values) != len(right_values):
            numeric_differences.append(
                {
                    "field": name,
                    "reason": "length-mismatch",
                    "baseline_length": len(left_values),
                    "candidate_length": len(right_values),
                }
            )
            continue
        for index, (left_value, right_value) in enumerate(
            zip(left_values, right_values)
        ):
            if left_value != right_value:
                if _close(left_value, right_value, atol=atol, rtol=rtol):
                    numeric_within_tolerance = True
                else:
                    numeric_differences.append(
                        {
                            "field": name,
                            "reason": "numeric-divergence",
                            "index": index,
                            "baseline": left_value,
                            "candidate": right_value,
                        }
                    )
                    break
    if numeric_differences:
        return {
            "id": case_id,
            "classification": "numerical_regression",
            "details": {"numeric_differences": numeric_differences},
        }
    if numeric_within_tolerance:
        return {
            "id": case_id,
            "classification": "numerical_difference_within_tolerance",
            "details": {"atol": atol, "rtol": rtol},
        }
    return {
        "id": case_id,
        "classification": "exact_match",
        "details": {"signature_type": left_signature[0]},
    }


def compare_documents(
    cases_document: Mapping[str, Any],
    baseline_document: Mapping[str, Any],
    candidate_document: Mapping[str, Any],
) -> dict[str, Any]:
    validate_cases_document(cases_document)
    validate_result_document(baseline_document, label="baseline")
    validate_result_document(candidate_document, label="candidate")
    baseline_cases = {case["id"]: case for case in baseline_document["cases"]}
    candidate_cases = {case["id"]: case for case in candidate_document["cases"]}
    rows = [
        compare_case(case, baseline_cases.get(case["id"]), candidate_cases.get(case["id"]))
        for case in cases_document["cases"]
    ]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["classification"]] = counts.get(row["classification"], 0) + 1
    primary = min(
        (row["classification"] for row in rows),
        key=lambda classification: CLASS_PRIORITY[classification],
    )
    if any(row["classification"] in INCONCLUSIVE_CLASSES for row in rows):
        status = "inconclusive"
    elif any(row["classification"] in FAIL_CLASSES for row in rows):
        status = "failed"
    else:
        status = "passed"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "primary_classification": primary,
        "counts": counts,
        "cases": rows,
    }


def render_report(
    comparison: Mapping[str, Any], *, baseline_label: str, candidate_label: str
) -> str:
    lines = [
        "# Correctness validation report",
        "",
        f"- Baseline: `{baseline_label}`",
        f"- Candidate: `{candidate_label}`",
        f"- Status: **{comparison['status']}**",
        f"- Primary classification: `{comparison['primary_classification']}`",
        "",
        "| Case | Classification |",
        "|---|---|",
    ]
    for row in comparison["cases"]:
        lines.append(f"| `{row['id']}` | `{row['classification']}` |")
    if isinstance(comparison.get("comparability"), Mapping):
        lines.append("")
        lines.extend(render_certificate_section(comparison["comparability"]))
    if isinstance(comparison.get("execution"), Mapping):
        lines.append("")
        lines.extend(render_identity_section(comparison["execution"]))
    lines.extend(["", "## Details", ""])
    for row in comparison["cases"]:
        lines.extend(
            [
                f"### {row['id']}",
                "",
                f"Classification: `{row['classification']}`",
                "",
                "```json",
                json.dumps(row["details"], ensure_ascii=False, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def validate_execution_block(document: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    """Require the `execution` identity a result must carry to be comparable."""
    execution = document.get("execution")
    if not isinstance(execution, Mapping):
        raise CorrectnessError(
            f"{label} result has no execution block; it does not record which "
            "engine_args, model, or service produced it, so the comparison "
            "could not attribute a difference to code. Produce results with "
            "the current remote_correctness_harness.py or "
            "aisbench_adapter.py normalize --execution"
        )
    engine_args = execution.get("engine_args")
    if not isinstance(engine_args, Mapping):
        raise CorrectnessError(f"{label} result execution.engine_args must be an object")
    return dict(execution)


def flatten_execution(execution: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten nested identity fields into dotted keys for a per-key diff."""
    flat: dict[str, Any] = {}
    for key, value in execution.items():
        if key in EXECUTION_NESTED_FIELDS and isinstance(value, Mapping):
            for nested_key, nested_value in value.items():
                flat[f"{key}.{nested_key}"] = nested_value
        else:
            flat[key] = value
    return flat


def execution_differences(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Every dotted identity key whose value differs between the two runs."""
    left = flatten_execution(baseline)
    right = flatten_execution(candidate)
    rows: list[dict[str, Any]] = []
    for key in sorted(set(left) | set(right)):
        left_value = left.get(key)
        right_value = right.get(key)
        if left_value != right_value:
            rows.append({"key": key, "baseline": left_value, "candidate": right_value})
    return rows


def check_run_identity(
    run_state: Mapping[str, Any],
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Refuse to compare two runs that differ in anything not declared at init.

    The result label must match the side it is passed as, both results must
    carry an execution block, and every execution difference must be listed in
    `allowed_differences`. An undeclared difference means the comparison would
    attribute a divergence to code that may instead come from eager/graph mode,
    tensor-parallel degree, or a different model.
    """
    problems: list[str] = []
    for side, document in (("baseline", baseline), ("candidate", candidate)):
        expected = run_state[f"{side}_label"]
        actual = document.get("label")
        if actual != expected:
            problems.append(
                f"{side} result label is {actual!r} but this run's {side} label "
                f"is {expected!r}"
            )
    if problems:
        raise CorrectnessError("; ".join(problems))
    baseline_execution = validate_execution_block(baseline, label="baseline")
    candidate_execution = validate_execution_block(candidate, label="candidate")
    differences = execution_differences(baseline_execution, candidate_execution)
    allowed = set(run_state.get("allowed_differences", []))
    undeclared = [row["key"] for row in differences if row["key"] not in allowed]
    if undeclared:
        raise CorrectnessError(
            "baseline and candidate executions differ in undeclared fields: "
            + ", ".join(undeclared)
            + "; a divergence could not be attributed to code. If the difference "
            "is the variable under test, declare it at init with "
            "--allowed-difference KEY; otherwise rerun the states identically"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "baseline": {"label": baseline["label"], "execution": baseline_execution},
        "candidate": {"label": candidate["label"], "execution": candidate_execution},
        "allowed_differences": sorted(allowed),
        "observed_differences": differences,
        "identical_execution": not differences,
    }


def _side_identity(
    *,
    run_id: str,
    manifest: Mapping[str, Any],
    document: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> Any:
    online = bool(execution.get("base_url"))
    parts = [
        identity_from_manifest_fields(
            run_id,
            workspace_snapshot=manifest.get("workspace_snapshot"),
            environment=manifest.get("environment"),
            model=manifest.get("model"),
            topology=manifest.get("topology"),
        ),
        identity_from_execution_block(execution, run_id=run_id, online=online),
    ]
    observation = document.get("observation")
    if isinstance(observation, Mapping) and observation:
        parts.append(identity_from_recorded_observation(run_id, observation))
    return merge_identities(*parts, run_id=run_id)


def build_comparability_certificate(
    run_state: Mapping[str, Any],
    manifest: Mapping[str, Any],
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    baseline_execution: Mapping[str, Any],
    candidate_execution: Mapping[str, Any],
) -> dict[str, Any]:
    run_id = str(run_state["run_id"])
    return issue_certificate(
        _side_identity(
            run_id=f"{run_id}-baseline",
            manifest=manifest,
            document=baseline,
            execution=baseline_execution,
        ),
        _side_identity(
            run_id=f"{run_id}-candidate",
            manifest=manifest,
            document=candidate,
            execution=candidate_execution,
        ),
        vary=run_state.get("allowed_differences", []),
        must_observe_prefixes=CORRECTNESS_MUST_OBSERVE,
    )


def render_certificate_section(certificate: Mapping[str, Any]) -> list[str]:
    lines = [
        "## Comparability certificate",
        "",
        f"- Verdict: `{certificate.get('verdict')}`",
    ]
    for reason in certificate.get("blocking_reasons", []):
        lines.append(f"- Blocking: {reason}")
    if certificate.get("confounders"):
        lines.append("- Confounders:")
        for row in certificate["confounders"]:
            lines.append(
                f"  - `{row['key']}`: baseline `{row['baseline']}` "
                f"({row['baseline_origin']}) vs candidate `{row['candidate']}` "
                f"({row['candidate_origin']})"
            )
    lines.extend(["", "```json"])
    lines.append(json.dumps(certificate, ensure_ascii=False, indent=2, sort_keys=True))
    lines.extend(["```", ""])
    return lines


def render_identity_section(identity: Mapping[str, Any]) -> list[str]:
    lines = ["## Execution identity", ""]
    if identity["identical_execution"]:
        lines.append("- Baseline and candidate executions are identical apart from code.")
    else:
        lines.append(
            "- Declared differences under test: "
            + ", ".join(f"`{key}`" for key in identity["allowed_differences"])
        )
        lines.append("- Observed differences:")
        for row in identity["observed_differences"]:
            lines.append(
                f"  - `{row['key']}`: baseline `{json.dumps(row['baseline'])}` "
                f"vs candidate `{json.dumps(row['candidate'])}`"
            )
        lines.append(
            "- A regression verdict on this run is attributable to the declared "
            "variable(s), not necessarily to the code change."
        )
    lines.extend(["", "```json"])
    lines.append(
        json.dumps(
            {
                side: identity[side]["execution"]
                for side in ("baseline", "candidate")
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    lines.extend(["```", ""])
    return lines


def compare_run(
    run_dir: Path,
    *,
    baseline_path: Path,
    candidate_path: Path,
    updated_at: str | None = None,
) -> dict[str, Any]:
    run_state = _load_json(run_dir / "run.json", "run state")
    if run_state.get("status") != "planned":
        raise CorrectnessError(f"run is already {run_state.get('status')}")
    cases = _load_json(run_dir / "cases.json", "cases")
    baseline = _load_json(baseline_path, "baseline result")
    candidate = _load_json(candidate_path, "candidate result")
    emit_progress("check-identity", baseline=str(baseline_path), candidate=str(candidate_path))
    identity = check_run_identity(run_state, baseline, candidate)
    manifest = load_manifest(run_dir / "manifest.json")
    certificate = build_comparability_certificate(
        run_state,
        manifest,
        baseline,
        candidate,
        identity["baseline"]["execution"],
        identity["candidate"]["execution"],
    )
    try:
        certificate = consume_certificate(certificate)
    except ComparabilityError as exc:
        blocked = exc.certificate if exc.certificate is not None else certificate
        _atomic_write_json(run_dir / "comparability-certificate.json", blocked)
        raise CorrectnessError(str(exc)) from exc
    _atomic_write_json(run_dir / "comparability-certificate.json", certificate)
    emit_progress("compare", cases=len(cases.get("cases", [])))
    comparison = compare_documents(cases, baseline, candidate)
    comparison["execution"] = identity
    comparison["comparability"] = certificate
    raw_dir = run_dir / "raw_outputs"
    raw_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(baseline_path, raw_dir / "baseline.json")
    shutil.copy2(candidate_path, raw_dir / "candidate.json")
    _atomic_write_json(run_dir / "execution.json", identity)
    _atomic_write_json(run_dir / "comparison.json", comparison)
    _atomic_write_text(
        run_dir / "report.md",
        render_report(
            comparison,
            baseline_label=run_state["baseline_label"],
            candidate_label=run_state["candidate_label"],
        ),
    )
    timestamp = updated_at or utc_now()
    run_state["status"] = comparison["status"]
    run_state["updated_at"] = timestamp
    _atomic_write_json(run_dir / "run.json", run_state)

    manifest = transition_status(manifest, "running", updated_at=timestamp)
    for name, kind, uri in (
        ("baseline-output", "raw-output", "raw_outputs/baseline.json"),
        ("candidate-output", "raw-output", "raw_outputs/candidate.json"),
        ("execution-identity", "execution-identity", "execution.json"),
        (
            "comparability-certificate",
            "comparability-certificate",
            "comparability-certificate.json",
        ),
        ("comparison", "comparison", "comparison.json"),
        ("report", "report", "report.md"),
    ):
        manifest = add_artifact(
            manifest,
            name=name,
            kind=kind,
            uri=uri,
            updated_at=timestamp,
        )
    manifest = transition_status(manifest, comparison["status"], updated_at=timestamp)
    write_manifest(run_dir / "manifest.json", manifest)
    return comparison


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorrectnessError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CorrectnessError(f"{label} must be a JSON object")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    init = subparsers.add_parser("init", help="initialize a correctness run")
    init.add_argument("--run-dir", required=True, type=Path)
    init.add_argument("--run-id", required=True)
    init.add_argument("--cases", required=True, type=Path)
    init.add_argument("--baseline-label", required=True)
    init.add_argument("--candidate-label", required=True)
    init.add_argument("--workspace-snapshot", default="{}")
    init.add_argument("--environment", default="{}")
    init.add_argument("--model", default="{}")
    init.add_argument("--topology", default="{}")
    init.add_argument("--baseline-command", action="append", default=[])
    init.add_argument("--candidate-command", action="append", default=[])
    init.add_argument(
        "--allowed-difference",
        action="append",
        default=[],
        metavar="KEY",
        help=(
            "execution identity key the two states are meant to differ in, e.g. "
            "engine_args.enforce_eager for an eager-versus-graph comparison; "
            "compare refuses any undeclared difference"
        ),
    )
    init.add_argument(
        "--parent-run-id",
        default=None,
        help="run_id of the change-validation plan this run is evidence for",
    )

    compare = subparsers.add_parser("compare", help="compare normalized outputs")
    compare.add_argument("--run-dir", required=True, type=Path)
    compare.add_argument("--baseline", required=True, type=Path)
    compare.add_argument("--candidate", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "init":
            emit_progress("initialize", run_dir=str(args.run_dir))
            run_state = init_run(
                args.run_dir,
                run_id=args.run_id,
                cases_path=args.cases,
                baseline_label=args.baseline_label,
                candidate_label=args.candidate_label,
                workspace_snapshot=_json_object(
                    args.workspace_snapshot, "workspace-snapshot"
                ),
                environment=_json_object(args.environment, "environment"),
                model=_json_object(args.model, "model"),
                topology=_json_object(args.topology, "topology"),
                baseline_command=args.baseline_command,
                candidate_command=args.candidate_command,
                allowed_differences=args.allowed_difference,
                parent_run_id=args.parent_run_id,
                code=manifest_code(ROOT),
            )
            payload = {
                "status": "created",
                "run_id": run_state["run_id"],
                "run_dir": str(args.run_dir.resolve()),
                "allowed_differences": run_state["allowed_differences"],
            }
        else:
            comparison = compare_run(
                args.run_dir,
                baseline_path=args.baseline,
                candidate_path=args.candidate,
            )
            payload = {
                "status": comparison["status"],
                "primary_classification": comparison["primary_classification"],
                "observed_differences": [
                    row["key"] for row in comparison["execution"]["observed_differences"]
                ],
                "comparison": str((args.run_dir / "comparison.json").resolve()),
                "report": str((args.run_dir / "report.md").resolve()),
            }
    except (CorrectnessError, RunManifestError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
