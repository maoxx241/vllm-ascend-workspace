#!/usr/bin/env python3
"""Manage the read-only local cache of the shared knowledge layer.

Sync is one-directional: this repo never writes to ``corpus/verified/``, and
the commons never writes into a fork. Downward flow is a periodic pull into a
read-only cache under untracked ``.vaws-local/knowledge/shared/``, which the
three-layer query client then mounts as the ``shared`` layer.

``import`` takes a local directory (typically ``corpus/verified/`` of a clone)
rather than fetching over the network. It reads any ``*.yaml`` in that zone,
takes kind from the validated document, and refuses project/unverified zones,
unverified entries, unresolved coordinates, and missing source identity. A
rejected or partial refresh leaves the last valid cache and its importer-owned
source policy in place. ``clear`` deletes that policy with the cache files;
the next import must bind again. The upstream pull tooling is not published
yet, so doing the transport here would mean inventing a protocol that is
about to be replaced; a local import keeps the cache contract testable
without pretending to own the sync.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_knowledge_client as client  # noqa: E402
import vaws_knowledge_shared as shared  # noqa: E402
import vaws_knowledge_v2 as v2  # noqa: E402

DEFAULT_SHARED_DIR = ROOT / ".vaws-local" / "knowledge" / "shared"
UPSTREAM_REPO = shared.DEFAULT_SOURCE_REPO


def emit_progress(phase: str, message: str) -> None:
    print(f"[{phase}] {message}", file=sys.stderr, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def do_status(shared_dir: Path) -> dict[str, Any]:
    capability = client.probe_capabilities(
        repo_root=ROOT,
        shared_dir=shared_dir,
    )["shared"]
    inspection = shared.inspect_shared_cache(shared_dir)
    return {
        "status": "passed",
        "layer": "shared",
        "cache": capability,
        "entry_count": len(inspection.get("entries") or []),
        "problems": inspection.get("problems") or [],
        "degraded": capability["status"] != shared.AVAILABLE,
        "effect": (
            "queries degrade to project+candidate and say so"
            if capability["status"] != shared.AVAILABLE
            else "shared layer is mounted read-only"
        ),
    }


def do_import(
    shared_dir: Path,
    source: Path,
    *,
    source_repo: str,
    source_ref: str,
    expect_repo: str | None,
    expect_ref: str | None,
) -> dict[str, Any]:
    identity_problems = shared.source_identity_problems(
        source_repo, source_ref, expect_repo=expect_repo, expect_ref=expect_ref
    )
    if identity_problems:
        return {
            "status": "failed",
            "error": identity_problems[0],
            "problems": identity_problems,
            "cache_preserved": True,
        }
    if not source.is_dir():
        return {
            "status": "failed",
            "error": f"source directory does not exist: {source}",
            "cache_preserved": True,
        }
    yaml_files = shared.iter_shared_yaml_files(source)
    if not yaml_files:
        return {
            "status": "failed",
            "error": f"no *{shared.YAML_SUFFIX} documents in {source}",
            "cache_preserved": True,
        }
    for path in yaml_files:
        emit_progress("validate", path.name)
    staged, problems = shared.load_shared_source_documents(source)
    if problems or len(staged) != len(yaml_files):
        return {
            "status": "failed",
            "error": "no eligible verified-zone documents to import",
            "problems": problems,
            "cache_preserved": True,
        }
    total_entries = sum(int(item["entries"]) for item in staged)
    bound_repo = expect_repo or source_repo
    bound_ref = expect_ref or source_ref
    metadata = {
        "schema_version": 1,
        "source_repo": source_repo,
        "source_ref": source_ref,
        "source_path": str(source),
        "pulled_at": utc_now(),
        "documents": [item["name"] for item in staged],
        "entry_count": total_entries,
        "read_only": True,
    }
    policy = shared.build_source_policy(
        expect_repo=bound_repo, expect_ref=bound_ref
    )
    try:
        shared.install_shared_cache(shared_dir, staged, metadata, policy)
    except OSError as exc:
        return {
            "status": "failed",
            "error": f"failed to install shared cache: {exc}",
            "cache_preserved": True,
        }
    for item in staged:
        emit_progress("import", item["name"])
    installed = shared.inspect_shared_cache(shared_dir)
    return {
        "status": "passed",
        "layer": "shared",
        "cache_dir": str(shared_dir),
        "documents": installed.get("documents") or metadata["documents"],
        "entry_count": total_entries,
        "problems": [],
        "metadata": installed.get("metadata") or metadata,
        "source_repo": source_repo,
        "source_ref": source_ref,
        "expected_source_repo": bound_repo,
        "expected_source_ref": bound_ref,
    }


def do_clear(shared_dir: Path) -> dict[str, Any]:
    """Remove cache documents, metadata, and the importer-owned source policy."""

    removed: list[str] = []
    if shared_dir.is_dir():
        for path in sorted(shared_dir.iterdir()):
            if path.is_file():
                path.chmod(0o644)
                path.unlink()
                removed.append(path.name)
    return {
        "status": "passed",
        "layer": "shared",
        "removed": removed,
        "policy_removed": shared.SHARED_SOURCE_POLICY in removed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-dir", type=Path, default=DEFAULT_SHARED_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        required=True,
        help="local corpus/verified directory of a vaws-knowledge checkout",
    )
    import_parser.add_argument(
        "--source-repo",
        required=True,
        help="declared source identity, e.g. vllm-ascend-workspace/vaws-knowledge",
    )
    import_parser.add_argument(
        "--source-ref",
        required=True,
        help="full 40-character commit SHA of the declared source",
    )
    import_parser.add_argument(
        "--expect-source-repo",
        default=None,
        help="optional configured source-repo expectation; mismatch is refused",
    )
    import_parser.add_argument(
        "--expect-source-ref",
        default=None,
        help="optional configured source-ref expectation; mismatch is refused",
    )
    subparsers.add_parser("clear")
    args = parser.parse_args(argv)

    try:
        if args.command == "status":
            payload = do_status(args.shared_dir)
        elif args.command == "import":
            payload = do_import(
                args.shared_dir,
                args.source,
                source_repo=args.source_repo,
                source_ref=args.source_ref,
                expect_repo=args.expect_source_repo,
                expect_ref=args.expect_source_ref,
            )
        else:
            payload = do_clear(args.shared_dir)
    except (OSError, v2.KnowledgeV2Error) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), flush=True)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if payload["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
