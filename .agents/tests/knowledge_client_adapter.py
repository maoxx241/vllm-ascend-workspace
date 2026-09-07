#!/usr/bin/env python3
"""Protocol adapter from the shared conformance runner to this client's APIs.

Reads one JSON entry or document on stdin and prints the runner contract on
stdout. Crashes and missing imports are left as process failures; they are
not turned into semantic ``reject`` tokens.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_knowledge_v2 as v2  # noqa: E402
import vaws_redaction as redaction  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: knowledge_client_adapter.py hash|payload|schema|redaction|export", file=sys.stderr)
        return 2
    operation = argv[0]
    payload = json.load(sys.stdin)
    if operation == "hash":
        print(v2.content_hash(payload))
        return 0
    if operation == "payload":
        print(v2.canonical_payload(payload))
        return 0
    if operation == "schema":
        try:
            v2.validate_document(payload, context="project")
        except v2.KnowledgeV2Error:
            print("reject")
        else:
            print("accept")
        return 0
    if operation == "redaction":
        findings = redaction.export_findings(redaction.scan(payload))
        print("reject" if findings else "accept")
        return 0
    if operation == "export":
        exported = v2.export_document(
            payload["kind"],
            payload["entries"],
            contributor="anonymous",
            origin_repo="example/fork",
            submitted_at="2026-09-07",
        )
        sys.stdout.write(v2.serialize_document(exported))
        return 0
    print(f"unknown operation: {operation}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
