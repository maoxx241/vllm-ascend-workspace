#!/usr/bin/env python3
"""Index Run Manifest v1 runs and decide whether two of them are comparable.

Individual Skills already write Run Manifest v1 files under their own
`.vaws-local/<skill>/` trees. What is missing is the view across them: which
runs exist, what code state each one used, and whether a pair of runs differs
only in the variable under test. Without that view, a difference in numbers
cannot be attributed, and the usual outcome is re-running work that was already
done.

This helper is entirely local and read-only with respect to the manifests.
Progress goes to stderr as ``__VAWS_PROGRESS__=<json>``; the final payload is a
single JSON object on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_run_manifest import (  # noqa: E402
    RunManifestError,
    TERMINAL_STATUSES,
    validate_manifest,
)

SCHEMA_VERSION = 1
DEFAULT_STATE_ROOT = ".vaws-local"
MANIFEST_NAMES = ("manifest.json",)

# Fields that must match for two runs to be comparable. Anything that differs
# here and is not the declared variable under test is a confounder: it can
# explain the delta on its own, so the comparison cannot attribute the delta to
# the intended change.
IDENTITY_FIELDS = ("workspace_snapshot", "environment", "model", "topology")


class LedgerError(RuntimeError):
    """Raised when the ledger cannot answer the question as asked."""


def emit_progress(phase: str, message: str) -> None:
    payload = {"phase": phase, "message": message}
    print(f"__VAWS_PROGRESS__={json.dumps(payload, sort_keys=True)}", file=sys.stderr)


def discover_manifests(root: Path) -> list[Path]:
    if not root.is_dir():
        raise LedgerError(f"state root does not exist: {root}")
    found: list[Path] = []
    for name in MANIFEST_NAMES:
        found.extend(path for path in root.rglob(name) if path.is_file())
    return sorted(set(found))


def flatten(prefix: str, value: Any) -> dict[str, str]:
    """Flatten nested manifest metadata into comparable scalar leaves."""
    if isinstance(value, Mapping):
        flat: dict[str, str] = {}
        for key in sorted(value):
            flat.update(flatten(f"{prefix}.{key}" if prefix else str(key), value[key]))
        return flat
    if isinstance(value, (list, tuple)):
        return {prefix: json.dumps(list(value), sort_keys=True)}
    return {prefix: "" if value is None else str(value)}


def read_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise RunManifestError("manifest must be a JSON object")
    validate_manifest(payload)
    return dict(payload)


def summarize(path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    identity = {field: flatten(field, manifest.get(field, {})) for field in IDENTITY_FIELDS}
    identity_leaves = {key: value for group in identity.values() for key, value in group.items()}
    return {
        "run_id": manifest["run_id"],
        "run_type": manifest["run_type"],
        "parent_run_id": manifest.get("parent_run_id"),
        "status": manifest["status"],
        "terminal": manifest["status"] in TERMINAL_STATUSES,
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "manifest_path": str(path),
        "artifact_count": len(manifest.get("artifacts", [])),
        "identity": identity_leaves,
        "identity_complete": bool(identity_leaves),
    }


def build_index(root: Path) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    unindexed: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    for path in discover_manifests(root):
        try:
            manifest = read_manifest(path)
        except (RunManifestError, json.JSONDecodeError, OSError) as exc:
            # A file named manifest.json that is not Run Manifest v1 is reported
            # rather than skipped silently: an unindexed run is a run whose code
            # state cannot be proven later, which is the problem this Skill exists
            # to surface.
            unindexed.append({"path": str(path), "reason": f"{type(exc).__name__}: {exc}"})
            continue
        summary = summarize(path, manifest)
        run_id = summary["run_id"]
        if run_id in seen:
            unindexed.append(
                {
                    "path": str(path),
                    "reason": f"duplicate run_id {run_id!r}, already indexed from {seen[run_id]}",
                }
            )
            continue
        seen[run_id] = str(path)
        runs.append(summary)
    runs.sort(key=lambda item: (item.get("created_at") or "", item["run_id"]))
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for run in runs:
        by_type[run["run_type"]] = by_type.get(run["run_type"], 0) + 1
        by_status[run["status"]] = by_status.get(run["status"], 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "state_root": str(root),
        "run_count": len(runs),
        "runs": runs,
        "by_run_type": dict(sorted(by_type.items())),
        "by_status": dict(sorted(by_status.items())),
        "unindexed": unindexed,
        "runs_without_identity": [
            run["run_id"] for run in runs if not run["identity_complete"]
        ],
    }


def select_run(index: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    for run in index["runs"]:
        if run["run_id"] == run_id:
            return dict(run)
    raise LedgerError(f"run {run_id!r} is not in the index under {index['state_root']}")


def compare_runs(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    vary: Iterable[str],
) -> dict[str, Any]:
    """Decide whether a delta between two runs can be attributed.

    Declared varying keys are the intended difference. Every other difference is
    a confounder, and a single confounder is enough to make the comparison
    unable to attribute a delta: the effect of the intended change and the
    effect of the confounder are not separable from two runs.
    """
    declared = list(dict.fromkeys(vary))
    left = dict(baseline["identity"])
    right = dict(candidate["identity"])
    keys = sorted(set(left) | set(right))

    differing = [
        {"key": key, "baseline": left.get(key), "candidate": right.get(key)}
        for key in keys
        if left.get(key) != right.get(key)
    ]
    intended = [item for item in differing if item["key"] in declared]
    confounders = [item for item in differing if item["key"] not in declared]
    declared_but_identical = [
        key for key in declared if key in keys and left.get(key) == right.get(key)
    ]
    declared_unknown = [key for key in declared if key not in keys]

    blocking: list[str] = []
    if not left or not right:
        blocking.append(
            "at least one run carries no identity metadata, so nothing can be compared"
        )
    if confounders:
        blocking.append(
            f"{len(confounders)} undeclared difference(s) can explain a delta on their own"
        )
    if not baseline["terminal"] or not candidate["terminal"]:
        blocking.append("at least one run has not reached a terminal status")
    if declared_unknown:
        blocking.append(
            f"declared varying key(s) absent from both runs: {', '.join(declared_unknown)}"
        )

    verdict = "comparable" if not blocking else "not-comparable"
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": verdict,
        "baseline": {
            "run_id": baseline["run_id"],
            "run_type": baseline["run_type"],
            "status": baseline["status"],
        },
        "candidate": {
            "run_id": candidate["run_id"],
            "run_type": candidate["run_type"],
            "status": candidate["status"],
        },
        "declared_varying": declared,
        "intended_differences": intended,
        "confounders": confounders,
        "declared_but_identical": declared_but_identical,
        "blocking_reasons": blocking,
        "interpretation": (
            "Any delta between these runs is attributable to the declared varying keys."
            if verdict == "comparable"
            else "A delta between these runs cannot be attributed to the declared "
            "change. Re-run with the confounders held constant."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Index Run Manifest v1 runs and check pairwise comparability.",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = {"--state-root": "workspace-local state root holding per-Skill run directories"}

    index = sub.add_parser("index", help="index every Run Manifest v1 under the state root", allow_abbrev=False)
    show = sub.add_parser("show", help="show one indexed run in full", allow_abbrev=False)
    compare = sub.add_parser(
        "compare", help="check whether two runs differ only in the declared variable", allow_abbrev=False
    )
    for target in (index, show, compare):
        for flag, help_text in common.items():
            target.add_argument(flag, default=DEFAULT_STATE_ROOT, help=help_text)
    index.add_argument("--run-type", action="append", default=[], help="restrict to these run types")
    index.add_argument("--status", action="append", default=[], help="restrict to these statuses")
    index.add_argument("--output", help="write the index JSON here as well as to stdout")
    show.add_argument("--run-id", required=True)
    compare.add_argument("--baseline", required=True, metavar="RUN_ID")
    compare.add_argument("--candidate", required=True, metavar="RUN_ID")
    compare.add_argument(
        "--vary",
        action="append",
        default=[],
        metavar="IDENTITY_KEY",
        help="dotted identity key expected to differ, e.g. workspace_snapshot.vllm_ascend_commit; "
        "repeat per key. Every other difference is reported as a confounder.",
    )
    compare.add_argument("--output", help="write the comparison JSON here as well as to stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = Path(args.state_root)
        emit_progress("index", f"scanning {root}")
        index = build_index(root)
        emit_progress("index", f"indexed {index['run_count']} run(s)")

        if args.command == "index":
            runs = index["runs"]
            if args.run_type:
                runs = [run for run in runs if run["run_type"] in set(args.run_type)]
            if args.status:
                runs = [run for run in runs if run["status"] in set(args.status)]
            payload = dict(index)
            payload["runs"] = runs
            payload["run_count"] = len(runs)
            result: dict[str, Any] = payload
        elif args.command == "show":
            result = select_run(index, args.run_id)
        else:
            result = compare_runs(
                select_run(index, args.baseline),
                select_run(index, args.candidate),
                vary=args.vary,
            )

        if getattr(args, "output", None):
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            result = dict(result)
            result["output_path"] = str(output.resolve())
        print(json.dumps(result, indent=2, sort_keys=True))
        if args.command == "compare" and result["verdict"] != "comparable":
            return 1
        return 0
    except LedgerError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
