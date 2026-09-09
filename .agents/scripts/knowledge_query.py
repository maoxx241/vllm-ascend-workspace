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
from importlib.metadata import PackageNotFoundError, version
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

REQUIRED_KNOWLEDGE = "0.1.4"


def _version_core(raw: str) -> tuple[int, ...]:
    core = raw.split("+", 1)[0]
    for marker in ("a", "b", "rc", ".dev"):
        idx = core.find(marker)
        if idx != -1:
            core = core[:idx]
    parts: list[int] = []
    for item in core.split("."):
        if item.isdigit():
            parts.append(int(item))
        else:
            break
    return tuple(parts) or (0,)


def require_knowledge_reader_cli() -> None:
    """Fail with cause and remedy when the coordinate CLI helpers are absent."""
    try:
        installed = version("vaws-knowledge")
    except PackageNotFoundError as exc:
        raise SystemExit(
            "vaws-knowledge is not installed; reader-coordinate flags cannot "
            "run. Install it with `uv sync`."
        ) from exc
    if _version_core(installed) < _version_core(REQUIRED_KNOWLEDGE):
        raise SystemExit(
            f"vaws-knowledge {installed} is too old for reader-coordinate "
            f"flags (need >={REQUIRED_KNOWLEDGE}). Run `uv sync`."
        )
    try:
        from vaws_knowledge.server.query import (  # noqa: F401
            add_reader_coordinate_arguments,
            reader_coordinate_from_args,
        )
    except ImportError as exc:
        raise SystemExit(
            "this vaws-knowledge install does not export the reader-coordinate "
            f"CLI helpers ({type(exc).__name__}: {exc}). Need "
            f">={REQUIRED_KNOWLEDGE}; run `uv sync`."
        ) from exc


def _explain(config, ident: str, layers: list[str] | None) -> dict:
    wanted = layers or list(LAYERS)
    report = load_entries(config, wanted)
    for loaded in report.entries:
        if loaded.uuid == ident or str(loaded.entry.get("slug") or "") == ident:
            return explain(config, loaded.uuid, layers=layers)
    return explain(config, ident, layers=layers)


def main(argv: list[str] | None = None) -> int:
    require_knowledge_reader_cli()
    from vaws_knowledge.server.query import (
        add_reader_coordinate_arguments,
        reader_coordinate_from_args,
    )

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
    add_reader_coordinate_arguments(parser)
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
    reader_coordinate = reader_coordinate_from_args(args)
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
            reader_coordinate=reader_coordinate,
        ).to_dict()
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
