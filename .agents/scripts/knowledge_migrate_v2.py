#!/usr/bin/env python3
"""Migrate v1 knowledge documents to the federated v2 contract.

Writes ``<kind>.v2.yaml`` next to the v1 documents and reports, per entry,
exactly which coordinate dimensions a human must still supply. The v1
documents are left in place: dual-read keeps existing consumers working while
the coordinates are being filled in.

Nothing is guessed. A dimension that cannot be derived mechanically from the
v1 entry becomes an explicit unresolved marker, which blocks both
``status: verified`` and export.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_knowledge_migrate as migrate  # noqa: E402
import vaws_knowledge_v2 as v2  # noqa: E402
import vaws_redaction as redaction  # noqa: E402
from vaws_knowledge import (  # noqa: E402
    KNOWLEDGE_FILES,
    KnowledgeError,
    load_knowledge_file,
    validate_knowledge_document,
)


def emit_progress(phase: str, message: str) -> None:
    print(f"[{phase}] {message}", file=sys.stderr, flush=True)


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# v1 -> v2 knowledge migration report",
        "",
        f"- migration date: {payload['migrated_at']}",
        f"- origin repo: `{payload['origin_repo']}`",
        f"- redaction profile: `{payload['redaction_profile']}`",
        f"- migrated entries: {payload['summary']['migrated']}",
        f"- blocked entries: {payload['summary']['blocked']}",
        "",
        "Migrated entries land as `status: unverified`. Every dimension below is an",
        "explicit unresolved marker, not a guess: the entry cannot become `verified`",
        "and cannot be exported until a human supplies the value.",
        "",
    ]
    for document in payload["documents"]:
        lines.append(f"## `{document['kind']}`")
        lines.append("")
        if document.get("output"):
            lines.append(f"Output: `{document['output']}`")
            lines.append("")
        if document.get("source_redaction_findings"):
            lines.append(
                "The v1 source document still carries "
                + ", ".join(f"`{rule}`" for rule in document["source_redaction_findings"])
                + " findings. They are not carried into v2; the v1 file itself has to"
                " be cleaned separately."
            )
            lines.append("")
        for entry in document["entries"]:
            lines.append(f"### `{entry.get('v1_id')}`")
            lines.append("")
            if entry.get("blocked"):
                lines.append("Not migrated:")
                lines.append("")
                for reason in entry["blocked"]:
                    lines.append(f"- {reason}")
                lines.append("")
                continue
            lines.append(f"- migrated as: `{entry['slug']}` (status `{entry['status']}`)")
            if entry.get("removed"):
                for note in entry["removed"]:
                    lines.append(f"- removed at migration: {note}")
            if entry.get("notes"):
                for note in entry["notes"]:
                    lines.append(f"- note: {note}")
            lines.append("- needs human input:")
            lines.append("")
            if entry["needs_human_input"]:
                for pending in entry["needs_human_input"]:
                    lines.append(f"  - `{pending['dimension']}` — {pending['needs']}")
            else:
                lines.append("  - none; coordinate is complete")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-dir", type=Path, default=ROOT / ".agents" / "knowledge")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--origin-repo",
        required=True,
        help="fork this entry would be exported from, e.g. owner/vllm-ascend-workspace",
    )
    parser.add_argument(
        "--contributor",
        default="anonymous",
        help="GitHub handle recorded in provenance; defaults to anonymous",
    )
    parser.add_argument("--kind", action="append", dest="kinds")
    parser.add_argument("--report", type=Path, help="write a markdown report to this path")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    knowledge_dir: Path = args.knowledge_dir
    output_dir: Path = args.output_dir or knowledge_dir
    selected = set(args.kinds) if args.kinds else set(KNOWLEDGE_FILES.values())
    unknown = sorted(selected - set(KNOWLEDGE_FILES.values()))
    if unknown:
        print(
            json.dumps({"status": "failed", "error": f"unknown kinds: {', '.join(unknown)}"}),
            flush=True,
        )
        return 1

    documents: list[dict[str, Any]] = []
    migrated_count = 0
    blocked_count = 0
    try:
        for filename, kind in KNOWLEDGE_FILES.items():
            if kind not in selected:
                continue
            source = knowledge_dir / filename
            if not source.is_file():
                emit_progress("read", f"{filename} missing; skipped")
                continue
            emit_progress("read", f"{filename}")
            document = load_knowledge_file(source)
            # Structure is enforced; the redaction screen is not, because the
            # documents that need migrating are exactly the ones carrying the
            # values migration has to strip. Per-entry reports say what was
            # removed, and `knowledge_validate.py` still fails on the v1 file
            # until it is cleaned there too.
            validate_knowledge_document(
                document,
                expected_kind=kind,
                path=str(source),
                screen_sensitive=False,
            )
            source_leaks = sorted(
                {
                    finding.rule
                    for finding in redaction.blocking_findings(
                        redaction.scan(document, path=filename)
                    )
                }
            )
            if source_leaks:
                emit_progress(
                    "redact",
                    f"{filename} still carries {', '.join(source_leaks)} in the v1 "
                    "document; not carried into v2, clean the v1 file separately",
                )
            if not document.get("entries"):
                emit_progress("skip", f"{filename} has no entries")
                continue

            target = output_dir / f"{kind}{v2.V2_SUFFIX}"
            existing = v2.load_document(target) if target.is_file() else None
            migrated, reports = migrate.migrate_document(
                document,
                kind=kind,
                origin_repo=args.origin_repo,
                contributor=args.contributor,
                existing=existing,
            )
            migrated_count += len(migrated["entries"])
            blocked_count += sum(1 for report in reports if report.get("blocked"))
            output: str | None = None
            if migrated["entries"] and not args.dry_run:
                v2.write_document(target, migrated, context=v2.PROJECT_LAYER)
                output = str(target.relative_to(ROOT)) if target.is_relative_to(ROOT) else str(target)
                emit_progress("write", f"{target.name}: {len(migrated['entries'])} entries")
            elif migrated["entries"]:
                emit_progress("dry-run", f"{target.name}: {len(migrated['entries'])} entries")
            documents.append(
                {
                    "kind": kind,
                    "source": filename,
                    "output": output,
                    "source_redaction_findings": source_leaks,
                    "entries": [
                        {key: value for key, value in report.items() if key != "entry"}
                        for report in reports
                    ],
                }
            )
    except (KnowledgeError, v2.KnowledgeV2Error, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), flush=True)
        return 1

    payload = {
        "status": "passed",
        "migrated_at": v2.today(),
        "origin_repo": args.origin_repo,
        "contributor": args.contributor,
        "redaction_profile": redaction.REDACTION_PROFILE,
        "dry_run": args.dry_run,
        "documents": documents,
        "summary": {
            "migrated": migrated_count,
            "blocked": blocked_count,
            "needs_human_input": sorted(
                {
                    pending["dimension"]
                    for document in documents
                    for entry in document["entries"]
                    for pending in entry.get("needs_human_input", [])
                }
            ),
        },
    }
    if args.report and not args.dry_run:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(render_report(payload), encoding="utf-8")
        payload["report"] = str(args.report)
        emit_progress("report", str(args.report))
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
