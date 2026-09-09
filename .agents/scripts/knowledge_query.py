#!/usr/bin/env python3
"""Thin CLI over ``vaws_knowledge.server.query``.

Shared is the packaged corpus. Project is ``.agents/knowledge``. Candidate is
``.vaws-local/knowledge/candidate``. Output is the commons ``QueryResponse``
envelope (or the ``explain`` payload for ``--id``).
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

from vaws_knowledge.server.layers import LAYERS, load_entries  # noqa: E402
from vaws_knowledge.server.query import explain, query  # noqa: E402
from vaws_knowledge_service import infer_repo_root, service_config  # noqa: E402


def _explain(config, ident: str, layers: list[str] | None) -> dict:
    wanted = layers or list(LAYERS)
    report = load_entries(config, wanted)
    for loaded in report.entries:
        if loaded.uuid == ident or str(loaded.entry.get("slug") or "") == ident:
            return explain(config, loaded.uuid, layers=layers)
    return explain(config, ident, layers=layers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--query")
    mode.add_argument("--id", help="explain one entry by uuid or slug")
    parser.add_argument("--kind")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--include-unverified",
        action="store_true",
        help="include unverified status and the candidate layer",
    )
    parser.add_argument(
        "--bodies",
        default=None,
        help="comma-separated body variants (rule,measurement). Default: both.",
    )
    parser.add_argument(
        "--layer",
        action="append",
        dest="layers",
        choices=list(LAYERS),
        help="layer to consult; repeatable. Default: shared+project.",
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=ROOT / ".agents" / "knowledge",
    )
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=ROOT / ".vaws-local" / "knowledge" / "candidate",
    )
    args = parser.parse_args(argv)
    bodies = (
        [item.strip() for item in args.bodies.split(",") if item.strip()]
        if args.bodies
        else None
    )
    knowledge_dir = args.knowledge_dir.resolve()
    repo_root = infer_repo_root(knowledge_dir, knowledge_dir.parent)
    config = service_config(
        repo_root,
        project_root=knowledge_dir,
        candidate_root=args.candidate_dir.resolve(),
    )
    if args.id:
        payload = _explain(config, args.id, args.layers)
    else:
        payload = query(
            config,
            text=args.query,
            layers=args.layers,
            include_unverified=args.include_unverified,
            kind=args.kind,
            bodies=bodies,
            limit=args.limit,
        ).to_dict()
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
