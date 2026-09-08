#!/usr/bin/env python3
"""Query compact knowledge summaries or fetch one entry by id.

Three layers behind one query surface: ``shared`` (read-only cache pulled from
the federated commons), ``project`` (``.agents/knowledge/``, v1 and v2
documents), and ``candidate`` (unreviewed local observations).

Backwards compatible on purpose. The v1 invocation
``--query "<text>" [--kind K] [--limit N] [--include-deprecated]`` returns the
same ``matches`` array with the same keys it always did, because other skills
and an ``AGENTS.md`` routing rule depend on it. New behaviour is additive:
matches gain ``layer`` / ``schema_version``, and the payload gains
``coverage``, ``degradation`` and ``capabilities``.

Degradation is always visible and never fatal. A missing shared cache or an
absent knowledge service narrows the answer and says so; an empty result is
reported as ``no-match``, which means *unknown*, never *supported*.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)


import vaws_knowledge_client as client  # noqa: E402
from vaws_knowledge_v1 import (  # noqa: E402
    KnowledgeError,
    get_knowledge_entry,
    query_knowledge,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--query")
    mode.add_argument("--id")
    mode.add_argument(
        "--capabilities",
        action="store_true",
        help="report which layers are available without querying",
    )
    parser.add_argument("--kind", action="append", dest="kinds")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--include-deprecated", action="store_true")
    parser.add_argument(
        "--include-unverified",
        action="store_true",
        help="also return v2 entries nobody else has confirmed",
    )
    parser.add_argument(
        "--layer",
        action="append",
        dest="layers",
        choices=list(client.LAYERS),
        help=(
            "layer to consult; repeatable. Default: shared+project. "
            "Omitting a layer is reported in coverage."
        ),
    )
    parser.add_argument(
        "--project-only",
        action="store_true",
        help="legacy single-layer behaviour: only .agents/knowledge/",
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=ROOT / ".agents" / "knowledge",
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=None,
        help="read-only shared cache directory (default .vaws-local/knowledge/shared)",
    )
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=None,
        help="local candidate queue (default .vaws-local/knowledge/candidates)",
    )
    args = parser.parse_args(argv)

    # A caller that points --knowledge-dir at another checkout (or a test
    # sandbox) means that tree, not this one: derive the sibling layer
    # locations from it so a query never mixes two repositories.
    repo_root = ROOT
    knowledge_dir = args.knowledge_dir.resolve()
    if knowledge_dir != (ROOT / ".agents" / "knowledge").resolve():
        if knowledge_dir.parent.name == ".agents":
            repo_root = knowledge_dir.parent.parent
        else:
            repo_root = knowledge_dir.parent

    try:
        if args.capabilities:
            payload = {
                "status": "passed",
                "capabilities": client.probe_capabilities(
                    repo_root=repo_root,
                    knowledge_dir=args.knowledge_dir,
                    shared_dir=args.shared_dir,
                    candidate_dir=args.candidate_dir,
                ),
                "unknown_semantics": client.UNKNOWN_SEMANTICS,
            }
        elif args.id:
            if args.project_only:
                result = get_knowledge_entry(
                    knowledge_dir=args.knowledge_dir, entry_id=args.id
                )
            else:
                result = client.get_entry(
                    repo_root=repo_root,
                    entry_id=args.id,
                    layers=args.layers or client.LAYERS,
                    knowledge_dir=args.knowledge_dir,
                    shared_dir=args.shared_dir,
                    candidate_dir=args.candidate_dir,
                )
            payload = {
                "status": "passed" if result else "not-found",
                "id": args.id,
                "result": result,
                "unknown_semantics": client.UNKNOWN_SEMANTICS,
            }
        elif args.project_only:
            matches = query_knowledge(
                knowledge_dir=args.knowledge_dir,
                query=args.query,
                kinds=args.kinds,
                limit=args.limit,
                include_deprecated=args.include_deprecated,
                include_unverified=args.include_unverified,
            )
            payload = {
                "status": "passed",
                "query": args.query,
                "matches": matches,
                "coverage": {
                    "layers_requested": ["project"],
                    "layers_answered": ["project"],
                    "layers_unavailable": [],
                    "result": "match" if matches else "no-match",
                },
                "unknown_semantics": client.UNKNOWN_SEMANTICS,
            }
        else:
            payload = client.query(
                repo_root=repo_root,
                query=args.query,
                layers=args.layers or client.DEFAULT_LAYERS,
                kinds=args.kinds,
                limit=args.limit,
                include_unverified=args.include_unverified,
                include_deprecated=args.include_deprecated,
                knowledge_dir=args.knowledge_dir,
                shared_dir=args.shared_dir,
                candidate_dir=args.candidate_dir,
            )
    except KnowledgeError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
