#!/usr/bin/env python3
"""Local self-test for the knowledge hooks in collect_torch_profile_case.py.

No remote container, no torch_npu: both hooks (``knowledge_preflight_advisories``
and ``knowledge_failure_matches``) are pure local functions over the workspace
knowledge store, so they are exercised here against a temporary knowledge dir
with synthetic entries.

Validates:

1. preflight advisory -- "<model> tp<N> <mode>" matches a synthetic
   model-capabilities entry (and a failure-signature entry), returning
   entry_id/kind/summary/score; empty store and missing dir both yield an
   explicit empty array without raising;
2. failure-gate enrichment -- an observed error text matching a synthetic
   known-failure-signatures entry returns the entry *with* its resolution;
   unrelated text, an empty store, and a missing dir all yield [];
3. ``_failure_payload`` shape -- {message, knowledge_matches}.

Run: python3 scripts/selftest_knowledge_hooks.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

from collect_torch_profile_case import (  # noqa: E402
    _failure_payload,
    knowledge_failure_matches,
    knowledge_preflight_advisories,
)

import vaws_knowledge_v2 as v2  # noqa: E402

KIND_FILES = {
    "known-failure-signatures.v2.yaml": "known-failure-signatures",
    "model-capabilities.v2.yaml": "model-capabilities",
}


def _scope(**values: str) -> dict:
    scope = {name: {"range": {"min": None, "max": None}} for name in v2.SCOPE_DIMENSIONS}
    for name, value in values.items():
        scope[name] = {"values": [value]}
    return scope


def _v2_entry(*, kind: str, slug: str, summary: str, fingerprints: list[str], resolution: str) -> dict:
    return v2.with_content_hash(
        {
            "uuid": v2.derived_uuid("owner/fork", kind, slug),
            "slug": slug,
            "content_hash": "sha256:" + "0" * 64,
            "status": "unverified",
            "confidence": "low",
            "scope": _scope(component="profiling"),
            "provenance": {
                "contributor": "submitter",
                "origin_repo": "owner/fork",
                "submitted_at": "2026-09-03",
                "redaction_profile": "r2",
            },
            "lifecycle": {
                "first_seen": "2026-09-03",
                "updated_at": "2026-09-03",
                "superseded_by": None,
                "resolved_by": None,
            },
            "rule": {
                "summary": summary,
                "symptom": summary,
                "root_cause": "recorded for the test fixture",
                "resolution": resolution,
                "fingerprints": fingerprints,
            },
        }
    )


MODEL_ENTRY = _v2_entry(
    kind="model-capabilities",
    slug="model-demomodel-7b",
    summary="DemoModel-7B: 40-layer test model; verified tp2 enforce_eager",
    fingerprints=["demomodel-7b"],
    resolution="Use the recorded tp2 enforce_eager configuration.",
)

FAILURE_ENTRY = _v2_entry(
    kind="known-failure-signatures",
    slug="demo-gloo-hostname",
    summary="gloo init fails when the container hostname is missing from /etc/hosts",
    fingerprints=["gloo makedeviceforhostname", "name or service not known hostname"],
    resolution="add 127.0.0.1 <hostname> to /etc/hosts before serve_start",
)

_FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _FAILURES.append(label)


def write_knowledge_dir(root: Path, entries_by_kind: dict[str, list[dict]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for filename, kind in KIND_FILES.items():
        document = {
            "schema_version": 2,
            "kind": kind,
            "layer": "unverified",
            "updated_at": "2026-09-03",
            "entries": entries_by_kind.get(kind, []),
        }
        v2.write_document(root / filename, document, context=v2.PROJECT_LAYER)
    return root


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="knowledge_hooks_selftest_"))
    print(f"selftest workspace: {tmp}")

    populated = write_knowledge_dir(
        tmp / "populated",
        {
            "model-capabilities": [MODEL_ENTRY],
            "known-failure-signatures": [FAILURE_ENTRY],
        },
    )
    empty = write_knowledge_dir(tmp / "empty", {})
    missing = tmp / "missing"

    # -- Preflight advisory ---------------------------------------------------
    advisories = knowledge_preflight_advisories(
        "DemoModel-7B", 2, "enforce_eager", knowledge_dir=populated
    )
    by_kind = {}
    for item in advisories:
        by_kind.setdefault(item["kind"], []).append(item)
    check(
        "preflight: model-capabilities hit with entry_id/summary/score",
        any(
            item["entry_id"] == "model-demomodel-7b"
            and item["summary"] == MODEL_ENTRY["rule"]["summary"]
            and item["score"] > 0
            for item in by_kind.get("model-capabilities", [])
        ),
        f"got {advisories}",
    )
    check(
        "preflight: all three kinds were queried (keys present in result kinds set)",
        all(
            item["kind"] in KIND_FILES.values() for item in advisories
        ),
    )

    check(
        "preflight: empty store -> explicit empty array",
        knowledge_preflight_advisories(
            "DemoModel-7B", 2, "enforce_eager", knowledge_dir=empty
        )
        == [],
    )
    check(
        "preflight: missing dir -> explicit empty array, no raise",
        knowledge_preflight_advisories(
            "DemoModel-7B", 2, "enforce_eager", knowledge_dir=missing
        )
        == [],
    )
    check(
        "preflight: unrelated model -> empty array",
        knowledge_preflight_advisories(
            "zz-unrelated", 8, "piecewise_graph", knowledge_dir=populated
        )
        == [],
    )

    # -- Failure-gate enrichment ----------------------------------------------
    matches = knowledge_failure_matches(
        "service did not become ready: gloo::makeDeviceForHostname failed: "
        "Name or service not known (hostname)",
        knowledge_dir=populated,
    )
    check(
        "failure: gloo signature matched with resolution attached",
        len(matches) >= 1
        and matches[0]["entry_id"] == "demo-gloo-hostname"
        and matches[0]["resolution"] == FAILURE_ENTRY["rule"]["resolution"]
        and matches[0]["kind"] == "known-failure-signatures"
        and matches[0]["score"] > 0,
        f"got {matches}",
    )
    check(
        "failure: unrelated text -> explicit empty array",
        knowledge_failure_matches("zz zz totally unrelated", knowledge_dir=populated)
        == [],
    )
    check(
        "failure: empty store -> explicit empty array",
        knowledge_failure_matches("gloo makeDeviceForHostname", knowledge_dir=empty)
        == [],
    )
    check(
        "failure: missing dir -> explicit empty array, no raise",
        knowledge_failure_matches("gloo makeDeviceForHostname", knowledge_dir=missing)
        == [],
    )

    # -- _failure_payload shape ------------------------------------------------
    payload = _failure_payload("gloo makeDeviceForHostname boom")
    # _failure_payload uses the real workspace knowledge dir; only the shape
    # is pinned here (matches may or may not hit the real store).
    check(
        "failure payload: {message, knowledge_matches} shape",
        set(payload) == {"message", "knowledge_matches"}
        and payload["message"] == "gloo makeDeviceForHostname boom"
        and isinstance(payload["knowledge_matches"], list),
        f"got {payload}",
    )

    print()
    if _FAILURES:
        print(f"SELFTEST FAILED: {len(_FAILURES)} check(s): {_FAILURES}")
        return 1
    print("SELFTEST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
