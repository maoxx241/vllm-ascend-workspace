#!/usr/bin/env python3
"""Validate all shared workspace knowledge documents.

Covers federated v2 ``<kind>.v2.yaml`` documents in ``.agents/knowledge/``.
Also reports the redaction posture of the project layer, split by severity:

- ``block`` findings (addresses, hostnames, user paths, secrets) fail
  validation — they must not be in a tracked file at all;
- ``export`` findings (internal mounts, container names, ticket ids) are legal
  project-layer facts and are reported as ``export_blockers`` instead, because
  some facts are genuinely not publishable and belong to this layer only.
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
from vaws_knowledge_service import KnowledgeError  # noqa: E402


def redaction_report(knowledge_dir: Path) -> dict[str, Any]:
    entries, problems = v2.load_entries(knowledge_dir, validate=False)
    export_blockers: list[dict[str, Any]] = []
    for entry in entries:
        payload = {key: value for key, value in entry.items() if not key.startswith("_")}
        for finding in redaction.scan(payload, path=str(entry.get("slug"))):
            if finding.severity == redaction.EXPORT:
                export_blockers.append(finding.to_dict())
    return {
        "profile": redaction.REDACTION_PROFILE,
        "export_blockers": export_blockers,
        "problems": problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=ROOT / ".agents" / "knowledge",
    )
    args = parser.parse_args(argv)
    try:
        files = sorted(path.name for path, _kind in v2.iter_documents(args.knowledge_dir))
        v2_entries, problems = v2.load_entries(args.knowledge_dir)
        if problems:
            raise KnowledgeError("\n".join(problems))
    except (KnowledgeError, v2.KnowledgeV2Error) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    unresolved = {
        str(entry.get("slug")): v2.unresolved_dimensions(entry)
        for entry in v2_entries
        if v2.unresolved_dimensions(entry)
    }
    hash_mismatches = []
    for entry in v2_entries:
        try:
            expected = commons_content_hash(entry)
        except ValueError:
            continue
        if entry.get("content_hash") != expected:
            hash_mismatches.append(str(entry.get("slug")))
    if hash_mismatches:
        print(json.dumps({"status": "failed", "error": "content_hash mismatch: " + ", ".join(hash_mismatches)}))
        return 1
    print(
        json.dumps(
            {
                "status": "passed",
                "knowledge_dir": str(args.knowledge_dir.resolve()),
                "files": files,
                "v2_documents": files,
                "v2_entries": len(v2_entries),
                "entries_awaiting_human_coordinate": unresolved,
                "redaction": redaction_report(args.knowledge_dir),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
