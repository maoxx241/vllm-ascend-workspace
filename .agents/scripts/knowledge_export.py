#!/usr/bin/env python3
"""Source-side export gate for proposing project knowledge upstream.

Redaction happens in the fork, before anything reaches a public PR, because
public git history cannot be recalled. This script is that gate:

1. select project-layer v2 entries by slug or uuid;
2. refuse any entry with an unresolved coordinate dimension;
3. re-stamp provenance (contributor, origin repo, redaction profile);
4. recompute ``content_hash`` over the canonicalized scope+rule payload;
5. validate against the v2 egress whitelist in export context;
6. run the full redaction ruleset, including ``export`` severity findings that
   are legal in the project layer but must never leave it;
7. write the proposal bundle under untracked ``.vaws-local/``.

Idempotency is keyed on ``uuid`` + ``content_hash`` against a local export
ledger: re-exporting an unchanged entry is reported as a no-op so it does not
turn into a second upstream PR.
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

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)


import vaws_knowledge_v2 as v2  # noqa: E402
import vaws_redaction as redaction  # noqa: E402
from vaws_knowledge.canonical import content_hash as commons_content_hash  # noqa: E402

DEFAULT_EXPORT_DIR = ROOT / ".vaws-local" / "knowledge" / "export"
LEDGER_NAME = "export-ledger.json"


def emit_progress(phase: str, message: str) -> None:
    print(f"[{phase}] {message}", file=sys.stderr, flush=True)


def load_ledger(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {str(key): str(value) for key, value in entries.items()}


def write_ledger(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema_version": 1, "entries": entries},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-dir", type=Path, default=ROOT / ".agents" / "knowledge")
    parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--origin-repo", required=True)
    parser.add_argument("--contributor", default="anonymous")
    parser.add_argument(
        "--entry",
        action="append",
        dest="entries",
        help="slug or uuid to export; repeatable. Omit to check every entry.",
    )
    parser.add_argument("--kind", action="append", dest="kinds")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report exportability without writing a bundle",
    )
    args = parser.parse_args(argv)

    emit_progress("read", str(args.knowledge_dir))
    entries, problems = v2.load_entries(args.knowledge_dir)
    selected_kinds = set(args.kinds) if args.kinds else None
    wanted = set(args.entries) if args.entries else None

    chosen: list[dict[str, Any]] = []
    for entry in entries:
        if selected_kinds is not None and entry.get("_kind") not in selected_kinds:
            continue
        if wanted is not None and not wanted & {entry.get("slug"), entry.get("uuid")}:
            continue
        chosen.append(entry)

    missing = sorted(
        wanted - {entry.get("slug") for entry in chosen} - {entry.get("uuid") for entry in chosen}
    ) if wanted else []

    ledger_path = args.export_dir / LEDGER_NAME
    ledger = load_ledger(ledger_path)

    exportable: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for entry in chosen:
        slug = entry.get("slug")
        try:
            prepared = v2.export_entry(
                entry,
                contributor=args.contributor,
                origin_repo=args.origin_repo,
            )
            if prepared["content_hash"] != commons_content_hash(prepared):
                raise v2.KnowledgeV2Error("content_hash disagrees with vaws_knowledge.canonical")
        except (v2.KnowledgeV2Error, redaction.RedactionError) as exc:
            emit_progress("blocked", f"{slug}: {exc}")
            blocked.append(
                {
                    "slug": slug,
                    "uuid": entry.get("uuid"),
                    "kind": entry.get("_kind"),
                    "unresolved_dimensions": v2.unresolved_dimensions(entry),
                    "reason": str(exc),
                }
            )
            continue
        record = {
            "slug": slug,
            "uuid": prepared["uuid"],
            "kind": entry.get("_kind"),
            "content_hash": prepared["content_hash"],
            "entry": prepared,
        }
        if ledger.get(prepared["uuid"]) == prepared["content_hash"]:
            emit_progress("no-op", f"{slug}: unchanged since last export")
            unchanged.append({key: value for key, value in record.items() if key != "entry"})
            continue
        exportable.append(record)

    bundle: dict[str, Any] | None = None
    if exportable and not args.check:
        stamp = v2.today()
        bundle_dir = args.export_dir / stamp
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for record in exportable:
            by_kind.setdefault(str(record["kind"]), []).append(record["entry"])
        written: list[str] = []
        for kind, kind_entries in sorted(by_kind.items()):
            document = v2.export_document(
                kind,
                kind_entries,
                contributor=args.contributor,
                origin_repo=args.origin_repo,
            )
            bundle_dir.mkdir(parents=True, exist_ok=True)
            path = bundle_dir / f"{kind}{v2.V2_SUFFIX}"
            path.write_text(v2.serialize_document(document), encoding="utf-8")
            written.append(str(path))
            emit_progress("write", str(path))
        for record in exportable:
            ledger[str(record["uuid"])] = str(record["content_hash"])
        write_ledger(ledger_path, ledger)
        bundle = {"dir": str(bundle_dir), "documents": written}

    # A requested slug that does not exist, or a document that failed to load,
    # is a failure of this run: exiting 0 would let a caller read "nothing was
    # exported" as "there was nothing to export".
    incomplete = bool(blocked or missing or problems)
    payload = {
        "status": "partial" if incomplete else "passed",
        "origin_repo": args.origin_repo,
        "contributor": args.contributor,
        "redaction_profile": redaction.REDACTION_PROFILE,
        "check_only": args.check,
        "exportable": [
            {key: value for key, value in record.items() if key != "entry"}
            for record in exportable
        ],
        "unchanged": unchanged,
        "blocked": blocked,
        "not_found": missing,
        "document_problems": problems,
        "bundle": bundle,
        "next_step": (
            "open a PR against vllm-ascend-workspace/vaws-knowledge; the bot lands a "
            "bot-approved entry in corpus/unverified/ and promotion to corpus/verified/ "
            "needs a followable evidence reference plus a non-submitter confirmation"
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
