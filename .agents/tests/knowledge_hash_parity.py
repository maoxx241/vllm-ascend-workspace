#!/usr/bin/env python3
"""Compare scaffold ``content_hash`` with ``vaws_knowledge.canonical.content_hash``.

Loads real entries from a vaws-knowledge checkout (``corpus/`` and
``examples/`` by default) and reports, per entry, whether the two
implementations agree byte for byte. A mismatch is a federation bug.

Usage:

    python3 .agents/tests/knowledge_hash_parity.py --commons /path/to/vaws-knowledge

The interpreter must have the published ``vaws-knowledge`` package installed.
The scaffold v1 store is ``vaws_knowledge_v1.py``, so it cannot shadow the
package. ``.agents/lib`` is still added only after the package import.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"


def _import_commons_hash():
    """Load the published package's ``content_hash``.

    Import the published package before ``.agents/lib`` is on ``sys.path``.
    The local v1 store is ``vaws_knowledge_v1.py`` and does not share this
    name, but keep the load order so a stale checkout cannot shadow it.
    """

    lib = str(LIB)
    if lib in sys.path:
        sys.path.remove(lib)
    sys.modules.pop("vaws_knowledge", None)
    sys.modules.pop("vaws_knowledge.canonical", None)
    try:
        from vaws_knowledge.canonical import content_hash
    except ImportError:
        return None
    return content_hash


commons_hash = _import_commons_hash()
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_knowledge_v2 as v2  # noqa: E402


def _load_yaml(path: Path) -> object:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment
        raise SystemExit(f"PyYAML is required to read {path}: {exc}") from exc
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _entries_of(payload: object) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
        return [item for item in payload["entries"] if isinstance(item, dict)]
    if isinstance(payload, dict) and "uuid" in payload:
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict) and "uuid" in item]
    return []


def collect_entries(commons: Path) -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []
    seen: set[str] = set()
    roots = [commons / "corpus", commons / "examples"]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.yaml")):
            try:
                payload = _load_yaml(path)
            except Exception as exc:
                print(f"skip {path}: {exc}", file=sys.stderr)
                continue
            for index, entry in enumerate(_entries_of(payload)):
                label = f"{path.relative_to(commons)}#{entry.get('uuid', index)}"
                key = str(entry.get("uuid") or label)
                if key in seen:
                    continue
                seen.add(key)
                found.append((label, entry))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commons",
        type=Path,
        required=True,
        help="checkout of vllm-ascend-workspace/vaws-knowledge",
    )
    parser.add_argument(
        "--min-entries",
        type=int,
        default=10,
        help="fail if fewer than this many comparable entries are loaded",
    )
    args = parser.parse_args(argv)

    if commons_hash is None:
        print(
            "cannot import vaws_knowledge.canonical; install the published "
            "vaws-knowledge package in this interpreter (uv sync)",
            file=sys.stderr,
        )
        return 2

    entries = collect_entries(args.commons)
    if len(entries) < args.min_entries:
        print(
            f"loaded {len(entries)} entries from {args.commons}, "
            f"need at least {args.min_entries}",
            file=sys.stderr,
        )
        return 2

    rows: list[dict[str, object]] = []
    mismatches = 0
    bodies = {"rule": 0, "measurement": 0, "other": 0}
    for label, entry in entries:
        body = v2.body_key(entry) or "other"
        bodies[body] = bodies.get(body, 0) + 1
        try:
            ours = v2.content_hash(entry)
            theirs = commons_hash(entry)
        except Exception as exc:
            mismatches += 1
            rows.append(
                {
                    "entry": label,
                    "body": body,
                    "ok": False,
                    "error": str(exc),
                }
            )
            print(f"DIFF  {label}  ({body})  error: {exc}")
            continue
        ok = ours == theirs
        if not ok:
            mismatches += 1
            print(f"DIFF  {label}  ({body})")
            print(f"      scaffold {ours}")
            print(f"      commons  {theirs}")
        else:
            print(f"SAME  {label}  ({body})  {ours}")
        rows.append(
            {
                "entry": label,
                "body": body,
                "ok": ok,
                "scaffold": ours,
                "commons": theirs,
            }
        )

    print()
    print(
        json.dumps(
            {
                "entries": len(entries),
                "rule": bodies.get("rule", 0),
                "measurement": bodies.get("measurement", 0),
                "mismatches": mismatches,
                "conclusion": (
                    f"{len(entries)} entries compared, {mismatches} 处不一致"
                    if mismatches
                    else f"{len(entries)} entries compared, 0 处不一致"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
