#!/usr/bin/env python3
"""Analyze a code diff and aggregate its actual validation evidence in one call."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_coordinator.run_manifest import (  # noqa: E402
    RunManifestError,
    add_artifact,
    load_manifest,
    new_manifest,
    transition_status,
    write_manifest,
)

SCHEMA_VERSION = 1
DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")


class ChangeValidationError(ValueError):
    """Raised when change-validation input or state is invalid."""


def _atomic_write(path: Path, text: str) -> None:
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


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChangeValidationError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ChangeValidationError(f"{label} root must be an object")
    return payload


def _run_git(repo_root: Path, arguments: Sequence[str]) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode != 0:
        raise ChangeValidationError(
            f"git {' '.join(arguments)} failed: {process.stderr.strip()}"
        )
    return process.stdout


def collect_git_diff(repo_root: Path, *, baseline: str, candidate: str) -> str:
    if candidate == "WORKTREE":
        diff = _run_git(repo_root, ["diff", "--no-ext-diff", "--unified=0", baseline, "--"])
        untracked = _run_git(
            repo_root, ["ls-files", "--others", "--exclude-standard"]
        ).splitlines()
        additions: list[str] = []
        for relative in sorted(path for path in untracked if path):
            path = repo_root / relative
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
            additions.extend(
                [
                    f"diff --git a/{relative} b/{relative}",
                    "new file mode 100644",
                    "--- /dev/null",
                    f"+++ b/{relative}",
                    "@@ -0,0 +1 @@",
                    *[f"+{line}" for line in text.splitlines()],
                ]
            )
        if additions:
            diff = diff.rstrip("\n") + "\n" + "\n".join(additions) + "\n"
        return diff
    return _run_git(
        repo_root,
        ["diff", "--no-ext-diff", "--unified=0", f"{baseline}...{candidate}", "--"],
    )


def parse_diff(diff_text: str) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    current: str | None = None
    changed_text: list[str] = []
    for line in diff_text.splitlines():
        header = DIFF_HEADER_RE.match(line)
        if header:
            current = header.group(2)
            files.setdefault(current, {"path": current, "additions": 0, "deletions": 0})
            continue
        if current is None:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            files[current]["additions"] += 1
            changed_text.append(line[1:])
        elif line.startswith("-"):
            files[current]["deletions"] += 1
            changed_text.append(line[1:])
    file_rows = sorted(files.values(), key=lambda row: row["path"])
    return {
        "schema_version": SCHEMA_VERSION,
        "file_count": len(file_rows),
        "additions": sum(row["additions"] for row in file_rows),
        "deletions": sum(row["deletions"] for row in file_rows),
        "files": file_rows,
        "matching_paths": "\n".join(row["path"] for row in file_rows),
        "matching_text": "\n".join(changed_text),
    }


def summarize_evidence(path: Path, *, baseline: str, candidate: str) -> dict[str, Any]:
    """Describe an existing run and the scope its recorded artifacts establish.

    Parent IDs and file-path keywords do not decide whether the evidence proves
    the requested change. Missing artifacts or attribution stay visible without
    preventing a report of the usable evidence.
    """
    from vaws_comparability import consume_certificate, ComparabilityError

    child = load_manifest(path)
    artifacts = {row["name"]: row for row in child.get("artifacts", [])}
    limitations: list[str] = []
    availability = []
    for name, artifact in artifacts.items():
        target = Path(artifact["uri"])
        if not target.is_absolute():
            target = path.parent / target
        exists = target.exists()
        availability.append({"name": name, "path": str(target), "available": exists})
        if not exists:
            limitations.append(f"artifact unavailable: {name}")

    def document(name: str) -> dict[str, Any]:
        item = next((row for row in availability if row["name"] == name), None)
        if item is None or not item["available"]:
            return {}
        try:
            return _load_json(Path(item["path"]), name)
        except ChangeValidationError as exc:
            limitations.append(str(exc))
            return {}

    comparison = document("comparison")
    certificate = document("comparability-certificate")
    revision_match = "unknown"
    observed_scope: dict[str, Any] = {}
    if certificate:
        try:
            certificate = consume_certificate(certificate)
        except ComparabilityError as exc:
            limitations.append(str(exc))
            certificate = exc.certificate or {}
        if certificate:
            matches = {}
            for side, revision in (("baseline", baseline), ("candidate", candidate)):
                observed = {
                    key: row["value"]
                    for key, row in certificate[side]["identity"].items()
                    if key.startswith("workspace_snapshot.") and row.get("origin") == "observed"
                    and row.get("value") not in (None, "")
                }
                observed_scope[side] = observed
                matches[side] = revision in observed.values()
            if not all(observed_scope.values()):
                limitations.append("recorded source revisions are missing for at least one side")
            elif all(matches.values()):
                revision_match = "matched"
            else:
                revision_match = "mismatched"
                limitations.append("recorded source revisions do not match the requested baseline/candidate")
    else:
        limitations.append("no existing comparability evidence establishes the requested code pair")
    if comparison and comparison.get("status") != child["status"]:
        limitations.append("comparison status disagrees with its manifest")
    if not artifacts:
        limitations.append("the run links no artifacts")
    if child["status"] not in {"passed", "failed", "inconclusive", "cancelled"}:
        limitations.append("the run has not completed")
    return {
        "run_id": child["run_id"],
        "run_type": child["run_type"],
        "status": child["status"],
        "manifest": str(path.resolve()),
        "revision_match": revision_match,
        "observed_scope": observed_scope,
        "artifacts": availability,
        "limitations": limitations,
    }


def render_report(summary: Mapping[str, Any]) -> str:
    lines = [
        "# Change validation evidence", "",
        f"- Goal: {summary['goal'] or 'Not provided'}",
        f"- Baseline: `{summary['baseline']}`",
        f"- Candidate: `{summary['candidate']}`",
        f"- Files changed: {summary['diff']['file_count']}",
        f"- Supplied evidence: **{summary['evidence_status']}**", "",
        "This report describes existing evidence. It does not determine which tests",
        "the change needs or establish complete validation from path keywords.",
        "The Agent assesses coverage against the changed behavior and its callers.", "",
        "| Run | Kind | Reported outcome | Source pair |",
        "|---|---|---|---|",
    ]
    for row in summary["runs"]:
        lines.append(f"| `{row['run_id']}` | {row['run_type']} | {row['status']} | {row['revision_match']} |")
    if not summary["runs"]:
        lines.extend(["", "No execution evidence was supplied. This does not create an extra test requirement."])
    for row in summary["runs"]:
        lines.extend(["", f"## {row['run_id']}", "", f"Manifest: `{row['manifest']}`"])
        lines.extend(f"- {reason}" for reason in row["limitations"])
        for artifact in row["artifacts"]:
            qualifier = "available" if artifact["available"] else "unavailable"
            lines.append(f"- {artifact['name']}: `{artifact['path']}` ({qualifier})")
    return "\n".join(lines) + "\n"


def build_report(*, diff_text: str, baseline: str, candidate: str, evidence=(),
                 goal="", target_repositories=(), output_dir=None, workspace_root=ROOT):
    from vaws_report import report_directory

    diff = parse_diff(diff_text)
    if not diff["files"]:
        raise ChangeValidationError("diff contains no changed files")
    diff.pop("matching_paths", None)
    diff.pop("matching_text", None)
    runs = [summarize_evidence(Path(path), baseline=baseline, candidate=candidate) for path in evidence]
    statuses = {row["status"] for row in runs}
    evidence_status = ("none" if not statuses else "failed" if "failed" in statuses
                       else "passed" if statuses == {"passed"} and not any(row["limitations"] for row in runs)
                       else "incomplete")
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "inconclusive",
        "evidence_status": evidence_status,
        "assessment": "Coverage of the changed behavior requires Agent judgment; this report imposes no test plan.",
        "baseline": baseline, "candidate": candidate, "goal": goal,
        "target_repositories": list(target_repositories), "diff": diff, "runs": runs,
    }
    output = report_directory(workspace_root, "vllm-ascend-change-validation", output_dir)
    if output.exists() and any(output.iterdir()):
        raise ChangeValidationError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    _atomic_write(output / "change.diff", diff_text)
    _write_json(output / "evidence-summary.json", summary)
    _atomic_write(output / "pr-validation-report.md", render_report(summary))
    manifest = new_manifest(run_type="change-validation", workspace_root=workspace_root,
                            workspace_snapshot={"baseline": baseline, "candidate": candidate})
    for name, kind, uri in (("diff", "diff", "change.diff"),
                            ("evidence-summary", "summary", "evidence-summary.json"),
                            ("pr-report", "report", "pr-validation-report.md")):
        manifest = add_artifact(manifest, name=name, kind=kind, uri=uri)
    for index, row in enumerate(runs):
        manifest = add_artifact(manifest, name=f"evidence-{index + 1}", kind="run-manifest", uri=row["manifest"])
    manifest = transition_status(manifest, "inconclusive")
    write_manifest(output / "manifest.json", manifest)
    return {"status": "inconclusive", "evidence_status": evidence_status, "linked_runs": len(runs),
            "assessment": summary["assessment"], "summary": str(output / "evidence-summary.json"),
            "report": str(output / "pr-validation-report.md"), "manifest": str(output / "manifest.json")}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--goal", default="")
    parser.add_argument("--target-repository", action="append", default=[])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--diff-file", type=Path)
    source.add_argument("--repo-root", type=Path)
    parser.add_argument("--evidence", nargs="*", type=Path, default=[])
    parser.add_argument("--output-dir", type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        workspace = args.repo_root.resolve() if args.repo_root else ROOT
        diff = args.diff_file.read_text(encoding="utf-8") if args.diff_file else collect_git_diff(
            workspace, baseline=args.baseline, candidate=args.candidate)
        revisions = [args.baseline, args.candidate]
        if args.repo_root:
            revisions = [subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "--verify", f"{ref}^{{commit}}"], text=True).strip() if ref != "WORKTREE" else ref for ref in revisions]
        result = build_report(diff_text=diff, baseline=revisions[0], candidate=revisions[1],
                              evidence=args.evidence, goal=args.goal, target_repositories=args.target_repository,
                              output_dir=args.output_dir, workspace_root=workspace)
    except (ChangeValidationError, RunManifestError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
