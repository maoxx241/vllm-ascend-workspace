"""Turn trial records into rates, intervals, and maturity verdicts.

The main product of the harness is the distinction between a *flake*
(intermittent: some repetitions pass, some fail) and a *failure*
(deterministic: every repetition fails), reported as a rate with a
confidence bound rather than a single verdict.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping

from .spec import OperationClass

VERDICTS = ("mature", "flaky", "broken", "insufficient", "slow")


def wilson_lower_bound(passes: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for a pass proportion."""
    if n <= 0:
        return 0.0
    phat = passes / n
    denominator = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denominator)


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def summarize_durations(values: Iterable[int | None]) -> dict[str, int | None]:
    clean = [int(v) for v in values if isinstance(v, (int, float))]
    if not clean:
        return {"min_ms": None, "median_ms": None, "p95_ms": None, "max_ms": None}
    return {
        "min_ms": min(clean),
        "median_ms": percentile(clean, 0.5),
        "p95_ms": percentile(clean, 0.95),
        "max_ms": max(clean),
    }


def classify(passes: int, n: int, op_class: OperationClass, p95_ms: int | None) -> str:
    if n == 0:
        return "insufficient"
    if passes == 0:
        return "broken"
    if passes < n:
        # Intermittent by definition. A high pass rate is still not mature:
        # 19/20 is a flake, not a boring command.
        return "flaky"
    if n < op_class.min_repetitions:
        return "insufficient"
    if op_class.p95_ms is not None and p95_ms is not None and p95_ms > op_class.p95_ms:
        return "slow"
    return "mature"


def _bucket(trials: Iterable[Mapping[str, Any]], op_class: OperationClass) -> dict[str, Any]:
    items = list(trials)
    n = len(items)
    passes = sum(1 for t in items if t.get("passed"))
    failures = [t for t in items if not t.get("passed")]
    durations = summarize_durations(t.get("duration_ms") for t in items)
    layers = Counter(str((t.get("attribution") or {}).get("layer") or "unknown") for t in failures)
    fingerprints = Counter(str((t.get("attribution") or {}).get("fingerprint") or "") for t in failures)
    return {
        "n": n,
        "passes": passes,
        "failures": n - passes,
        "pass_rate": round(passes / n, 4) if n else None,
        "pass_rate_lower_95": round(wilson_lower_bound(passes, n), 4) if n else None,
        "verdict": classify(passes, n, op_class, durations["p95_ms"]),
        "threshold": op_class.pass_rate_threshold,
        "min_repetitions": op_class.min_repetitions,
        "meets_threshold": bool(n and passes / n >= op_class.pass_rate_threshold and n >= op_class.min_repetitions),
        "durations": durations,
        "failure_layers": dict(layers),
        "failure_fingerprints": dict(fingerprints.most_common(5)),
        "failed_trial_ids": [str(t.get("trial_id")) for t in failures][:50],
    }


def aggregate(trials: list[Mapping[str, Any]], classes: Mapping[str, OperationClass]) -> dict[str, Any]:
    by_operation: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_operation_endpoint: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    class_of: dict[str, str] = {}
    shape_of: dict[str, str] = {}
    for trial in trials:
        op_id = str(trial.get("operation_id"))
        by_operation[op_id].append(trial)
        by_operation_endpoint[(op_id, str(trial.get("endpoint_label")))].append(trial)
        class_of[op_id] = str(trial.get("op_class"))
        shape_of[op_id] = str(trial.get("shape"))

    operations: dict[str, Any] = {}
    for op_id, items in sorted(by_operation.items()):
        op_class = classes[class_of[op_id]]
        per_endpoint = {
            label: _bucket(sub, op_class)
            for (candidate, label), sub in sorted(by_operation_endpoint.items())
            if candidate == op_id
        }
        operations[op_id] = {
            "class": op_class.name,
            "shape": shape_of[op_id],
            **_bucket(items, op_class),
            "per_endpoint": per_endpoint,
        }

    by_class: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trial in trials:
        by_class[str(trial.get("op_class"))].append(trial)
    class_summary = {
        name: {
            key: value
            for key, value in _bucket(items, classes[name]).items()
            if key in {"n", "passes", "failures", "pass_rate", "pass_rate_lower_95", "failure_layers"}
        }
        for name, items in sorted(by_class.items())
        if name in classes
    }

    total = len(trials)
    total_pass = sum(1 for t in trials if t.get("passed"))
    all_layers = Counter(
        str((t.get("attribution") or {}).get("layer") or "unknown") for t in trials if not t.get("passed")
    )
    return {
        "totals": {
            "trials": total,
            "passes": total_pass,
            "failures": total - total_pass,
            "pass_rate": round(total_pass / total, 4) if total else None,
            "failure_layers": dict(all_layers),
        },
        "classes": class_summary,
        "operations": operations,
        "ranking": rank_immaturity(operations),
    }


def rank_immaturity(operations: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Least mature first: broken, then flaky by failure rate, then slow/insufficient."""
    order = {"broken": 0, "flaky": 1, "slow": 2, "insufficient": 3, "mature": 4}

    def key(item: tuple[str, Mapping[str, Any]]) -> tuple[int, float, str]:
        op_id, summary = item
        rate = summary.get("pass_rate")
        return (order.get(str(summary.get("verdict")), 5), float(rate if rate is not None else 1.0), op_id)

    ranked = []
    for op_id, summary in sorted(operations.items(), key=key):
        ranked.append(
            {
                "operation_id": op_id,
                "class": summary.get("class"),
                "verdict": summary.get("verdict"),
                "pass_rate": summary.get("pass_rate"),
                "n": summary.get("n"),
                "failure_layers": summary.get("failure_layers", {}),
            }
        )
    return ranked
