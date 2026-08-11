#!/usr/bin/env python3
"""Plan and aggregate evidence for vLLM Ascend code changes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[4]
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VALIDATION_RULES = SKILL_ROOT / "references" / "validation-rules.yaml"
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_knowledge import load_knowledge_file  # noqa: E402
from vaws_run_manifest import (  # noqa: E402
    RunManifestError,
    add_artifact,
    load_manifest,
    new_manifest,
    transition_status,
    write_manifest,
)

SCHEMA_VERSION = 1
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
PLAN_PRIORITIES = frozenset({"required", "recommended"})


class ChangeValidationError(ValueError):
    """Raised when change-validation input or state is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def emit_progress(phase: str, **details: Any) -> None:
    print(json.dumps({"phase": phase, **details}, ensure_ascii=False), file=sys.stderr)


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
        "sha256": hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
        "file_count": len(file_rows),
        "additions": sum(row["additions"] for row in file_rows),
        "deletions": sum(row["deletions"] for row in file_rows),
        "files": file_rows,
        "matching_paths": "\n".join(row["path"] for row in file_rows),
        "matching_text": "\n".join(changed_text),
    }


def _safe_item_id(check: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", check.lower()).strip("-")
    if not slug:
        raise ChangeValidationError(f"cannot generate plan id from {check!r}")
    return slug[:120]


def build_plan(
    diff_summary: Mapping[str, Any],
    knowledge_document: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    path_haystack = str(diff_summary.get("matching_paths", ""))
    content_haystack = str(diff_summary.get("matching_text", ""))
    impacts: list[dict[str, Any]] = []
    item_map: dict[str, dict[str, Any]] = {}
    routes: list[dict[str, str]] = []
    for entry in knowledge_document.get("entries", []):
        if not isinstance(entry, Mapping) or entry.get("status") == "deprecated":
            continue
        rule = entry.get("rule", {})
        if not isinstance(rule, Mapping):
            continue
        path_patterns = rule.get("path_patterns", [])
        content_patterns = rule.get("content_patterns", [])
        matched_path_patterns = [
            pattern
            for pattern in path_patterns
            if isinstance(pattern, str) and re.search(pattern, path_haystack)
        ]
        matched_content_patterns = [
            pattern
            for pattern in content_patterns
            if isinstance(pattern, str) and re.search(pattern, content_haystack)
        ]
        if not matched_path_patterns and not matched_content_patterns:
            continue
        category = str(rule.get("category", entry.get("id", "uncategorized")))
        impacts.append(
            {
                "category": category,
                "rule_id": entry["id"],
                "matched_path_patterns": matched_path_patterns,
                "matched_content_patterns": matched_content_patterns,
                "source": entry.get("source"),
                "status": entry.get("status"),
            }
        )
        for priority, field in (
            ("required", "required_checks"),
            ("recommended", "recommended_checks"),
        ):
            for check in rule.get(field, []):
                item_id = _safe_item_id(str(check))
                item = item_map.setdefault(
                    item_id,
                    {
                        "id": item_id,
                        "check": str(check),
                        "priority": priority,
                        "sources": [],
                        "rationale": [],
                        "status": "planned",
                    },
                )
                if priority == "required":
                    item["priority"] = "required"
                item["sources"].append(entry["id"])
                item["rationale"].append(category)
        for key, value in rule.items():
            if key.startswith("route_on_") and isinstance(value, str):
                routes.append({"condition": key.removeprefix("route_on_"), "skill": value})

    manual_review = False
    if not item_map:
        manual_review = True
        item_map["correctness-targeted-smoke"] = {
            "id": "correctness-targeted-smoke",
            "check": "correctness:targeted-smoke",
            "priority": "required",
            "sources": ["fallback-no-rule-match"],
            "rationale": ["unclassified-change"],
            "status": "planned",
        }
    for item in item_map.values():
        item["sources"] = sorted(set(item["sources"]))
        item["rationale"] = sorted(set(item["rationale"]))
    impact_document = {
        "schema_version": SCHEMA_VERSION,
        "manual_review_required": manual_review,
        "impacts": sorted(impacts, key=lambda row: (row["category"], row["rule_id"])),
        "routes": sorted(
            {f"{route['condition']}:{route['skill']}": route for route in routes}.values(),
            key=lambda row: (row["condition"], row["skill"]),
        ),
    }
    plan_document = {
        "schema_version": SCHEMA_VERSION,
        "manual_review_required": manual_review,
        "items": sorted(
            item_map.values(),
            key=lambda item: (item["priority"] != "required", item["id"]),
        ),
    }
    return impact_document, plan_document


def render_report(
    *,
    run_state: Mapping[str, Any],
    diff_summary: Mapping[str, Any],
    impact: Mapping[str, Any],
    plan: Mapping[str, Any],
    links: Mapping[str, Any],
    final_status: str,
) -> str:
    coverage: dict[str, list[Mapping[str, Any]]] = {}
    for link in links.get("runs", []):
        for item_id in link.get("covers", []):
            coverage.setdefault(item_id, []).append(link)
    lines = [
        "# PR validation report",
        "",
        "## Change summary",
        "",
        f"- Goal: {run_state.get('goal') or 'Not provided'}",
        f"- Baseline: `{run_state['baseline']}`",
        f"- Candidate: `{run_state['candidate']}`",
        f"- Target repositories: {', '.join(run_state.get('target_repositories', [])) or 'Not provided'}",
        f"- Files changed: {diff_summary['file_count']}",
        f"- Lines: +{diff_summary['additions']} / -{diff_summary['deletions']}",
        f"- Validation status: **{final_status}**",
        "",
        "## Impact modules",
        "",
    ]
    if impact.get("impacts"):
        for row in impact["impacts"]:
            lines.append(f"- `{row['category']}` via `{row['rule_id']}`")
    else:
        lines.append("- No domain rule matched; manual review is required.")
    lines.extend(
        [
            "",
            "## Validation matrix",
            "",
            "| Requirement | Priority | Evidence | Result |",
            "|---|---|---|---|",
        ]
    )
    for item in plan["items"]:
        evidence = coverage.get(item["id"], [])
        evidence_text = ", ".join(f"`{row['run_id']}`" for row in evidence) or "missing"
        statuses = ", ".join(str(row["status"]) for row in evidence) or "not run"
        lines.append(
            f"| `{item['check']}` | {item['priority']} | {evidence_text} | {statuses} |"
        )
    lines.extend(["", "## Known limitations and missing coverage", ""])
    missing = [
        item
        for item in plan["items"]
        if item["priority"] == "required" and not coverage.get(item["id"])
    ]
    nonpassing = [
        link for link in links.get("runs", []) if link.get("status") != "passed"
    ]
    if not missing and not nonpassing and not plan.get("manual_review_required"):
        lines.append("- None recorded.")
    for item in missing:
        lines.append(f"- Required evidence missing: `{item['check']}`.")
    for link in nonpassing:
        lines.append(
            f"- Linked run `{link['run_id']}` is `{link['status']}` and does not prove acceptance."
        )
    if plan.get("manual_review_required"):
        lines.append("- Change classification requires manual review.")
    lines.extend(["", "## Reproduction and artifacts", ""])
    for link in links.get("runs", []):
        lines.append(f"- `{link['run_id']}`: `{link['manifest']}`")
    return "\n".join(lines) + "\n"


def plan_change(
    output_dir: Path,
    *,
    run_id: str,
    baseline: str,
    candidate: str,
    goal: str,
    target_repositories: Sequence[str],
    diff_text: str,
    knowledge_path: Path,
    created_at: str | None = None,
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ChangeValidationError(f"output directory is not empty: {output_dir}")
    if not SAFE_ID_RE.fullmatch(run_id):
        raise ChangeValidationError("run-id must be a lowercase safe identifier")
    knowledge = load_knowledge_file(knowledge_path)
    diff_summary = parse_diff(diff_text)
    if diff_summary["file_count"] == 0:
        raise ChangeValidationError("diff contains no changed files")
    impact, validation_plan = build_plan(diff_summary, knowledge)
    timestamp = created_at or utc_now()
    run_state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "baseline": baseline,
        "candidate": candidate,
        "goal": goal,
        "target_repositories": list(target_repositories),
        "status": "planned",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    links = {"schema_version": SCHEMA_VERSION, "runs": []}
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "run.json", run_state)
    stored_summary = dict(diff_summary)
    stored_summary.pop("matching_paths", None)
    stored_summary.pop("matching_text", None)
    _write_json(output_dir / "diff-summary.json", stored_summary)
    _write_json(output_dir / "impact-analysis.json", impact)
    _write_json(output_dir / "validation-plan.json", validation_plan)
    _write_json(output_dir / "linked-runs.json", links)
    report = render_report(
        run_state=run_state,
        diff_summary=stored_summary,
        impact=impact,
        plan=validation_plan,
        links=links,
        final_status="planned",
    )
    _atomic_write(output_dir / "pr-validation-report.md", report)
    manifest = new_manifest(
        run_type="change-validation",
        run_id=run_id,
        workspace_snapshot={
            "baseline": baseline,
            "candidate": candidate,
            "diff_sha256": diff_summary["sha256"],
        },
        created_at=timestamp,
    )
    for name, kind, uri in (
        ("diff-summary", "diff-summary", "diff-summary.json"),
        ("impact-analysis", "impact-analysis", "impact-analysis.json"),
        ("validation-plan", "validation-plan", "validation-plan.json"),
        ("linked-runs", "linked-runs", "linked-runs.json"),
        ("pr-report", "report", "pr-validation-report.md"),
    ):
        manifest = add_artifact(
            manifest, name=name, kind=kind, uri=uri, updated_at=timestamp
        )
    write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": "planned",
        "run_id": run_id,
        "file_count": diff_summary["file_count"],
        "impact_count": len(impact["impacts"]),
        "required_count": sum(
            item["priority"] == "required" for item in validation_plan["items"]
        ),
        "recommended_count": sum(
            item["priority"] == "recommended" for item in validation_plan["items"]
        ),
    }


def link_run(
    output_dir: Path,
    *,
    child_manifest_path: Path,
    covers: Sequence[str],
    updated_at: str | None = None,
) -> dict[str, Any]:
    plan = _load_json(output_dir / "validation-plan.json", "validation plan")
    valid_ids = {item["id"] for item in plan["items"]}
    unknown = sorted(set(covers) - valid_ids)
    if unknown:
        raise ChangeValidationError(f"unknown plan item ids: {', '.join(unknown)}")
    if not covers:
        raise ChangeValidationError("at least one --covers item is required")
    child = load_manifest(child_manifest_path)
    parent = load_manifest(output_dir / "manifest.json")
    if child.get("parent_run_id") not in {None, parent["run_id"]}:
        raise ChangeValidationError(
            f"child parent_run_id belongs to another run: {child.get('parent_run_id')}"
        )
    links = _load_json(output_dir / "linked-runs.json", "linked runs")
    if any(link["run_id"] == child["run_id"] for link in links["runs"]):
        raise ChangeValidationError(f"child run is already linked: {child['run_id']}")
    timestamp = updated_at or utc_now()
    link = {
        "run_id": child["run_id"],
        "run_type": child["run_type"],
        "status": child["status"],
        "manifest": str(child_manifest_path.resolve()),
        "covers": sorted(set(covers)),
        "linked_at": timestamp,
    }
    links["runs"].append(link)
    links["runs"].sort(key=lambda row: row["run_id"])
    _write_json(output_dir / "linked-runs.json", links)
    parent = add_artifact(
        parent,
        name=f"linked-run-{child['run_id']}",
        kind="linked-run-manifest",
        uri=str(child_manifest_path.resolve()),
        updated_at=timestamp,
    )
    write_manifest(output_dir / "manifest.json", parent)
    return link


def finalize(
    output_dir: Path, *, updated_at: str | None = None
) -> dict[str, Any]:
    run_state = _load_json(output_dir / "run.json", "run state")
    if run_state.get("status") != "planned":
        raise ChangeValidationError(f"run is already {run_state.get('status')}")
    diff_summary = _load_json(output_dir / "diff-summary.json", "diff summary")
    impact = _load_json(output_dir / "impact-analysis.json", "impact analysis")
    plan = _load_json(output_dir / "validation-plan.json", "validation plan")
    links = _load_json(output_dir / "linked-runs.json", "linked runs")
    coverage: dict[str, list[Mapping[str, Any]]] = {}
    for link in links["runs"]:
        for item_id in link["covers"]:
            coverage.setdefault(item_id, []).append(link)
    required = [item for item in plan["items"] if item["priority"] == "required"]
    missing = [item["id"] for item in required if not coverage.get(item["id"])]
    child_statuses = {link["status"] for link in links["runs"]}
    if "failed" in child_statuses:
        status = "failed"
    elif (
        missing
        or plan.get("manual_review_required")
        or any(child_status != "passed" for child_status in child_statuses)
    ):
        status = "inconclusive"
    else:
        status = "passed"
    for item in plan["items"]:
        evidence = coverage.get(item["id"], [])
        item["status"] = (
            "passed"
            if evidence and all(link["status"] == "passed" for link in evidence)
            else "missing"
            if not evidence
            else "nonpassing"
        )
    _write_json(output_dir / "validation-plan.json", plan)
    timestamp = updated_at or utc_now()
    run_state["status"] = status
    run_state["updated_at"] = timestamp
    _write_json(output_dir / "run.json", run_state)
    _atomic_write(
        output_dir / "pr-validation-report.md",
        render_report(
            run_state=run_state,
            diff_summary=diff_summary,
            impact=impact,
            plan=plan,
            links=links,
            final_status=status,
        ),
    )
    manifest = load_manifest(output_dir / "manifest.json")
    manifest = transition_status(manifest, "running", updated_at=timestamp)
    manifest = transition_status(manifest, status, updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": status,
        "run_id": run_state["run_id"],
        "missing_required": missing,
        "linked_runs": len(links["runs"]),
        "report": str((output_dir / "pr-validation-report.md").resolve()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan_parser = subparsers.add_parser("plan", help="analyze a diff and create a plan")
    plan_parser.add_argument("--output-dir", required=True, type=Path)
    plan_parser.add_argument("--run-id", required=True)
    plan_parser.add_argument("--baseline", required=True)
    plan_parser.add_argument("--candidate", default="WORKTREE")
    plan_parser.add_argument("--goal", default="")
    plan_parser.add_argument("--target-repository", action="append", default=[])
    source = plan_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--diff-file", type=Path)
    source.add_argument("--repo-root", type=Path)
    plan_parser.add_argument(
        "--knowledge",
        type=Path,
        default=DEFAULT_VALIDATION_RULES,
    )

    link_parser = subparsers.add_parser("link", help="link a downstream run")
    link_parser.add_argument("--output-dir", required=True, type=Path)
    link_parser.add_argument("--run-manifest", required=True, type=Path)
    link_parser.add_argument("--covers", action="append", required=True)

    finalize_parser = subparsers.add_parser("finalize", help="finalize coverage and report")
    finalize_parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "plan":
            emit_progress("collect-diff", baseline=args.baseline, candidate=args.candidate)
            if args.diff_file is not None:
                diff_text = args.diff_file.read_text(encoding="utf-8")
            else:
                diff_text = collect_git_diff(
                    args.repo_root.resolve(),
                    baseline=args.baseline,
                    candidate=args.candidate,
                )
            payload = plan_change(
                args.output_dir,
                run_id=args.run_id,
                baseline=args.baseline,
                candidate=args.candidate,
                goal=args.goal,
                target_repositories=args.target_repository,
                diff_text=diff_text,
                knowledge_path=args.knowledge,
            )
        elif args.action == "link":
            emit_progress("link-run", manifest=str(args.run_manifest))
            link = link_run(
                args.output_dir,
                child_manifest_path=args.run_manifest,
                covers=args.covers,
            )
            payload = {"status": "linked", "run": link}
        else:
            emit_progress("finalize", output_dir=str(args.output_dir))
            payload = finalize(args.output_dir)
    except (ChangeValidationError, RunManifestError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
