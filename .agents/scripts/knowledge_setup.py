#!/usr/bin/env python3
"""Initialize this workspace's corpus fork and package-owned publishing config."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)


def main() -> int:
    from vaws_knowledge.publishing import DEFAULT_CORPUS, configure
    from vaws_knowledge.distribution.manifest import atomic_write_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=DEFAULT_CORPUS)
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()
    config = ROOT / ".vaws-local/knowledge/service.json"
    if not config.exists():
        atomic_write_json(config, {
            "state_root": str(ROOT / ".vaws-local/knowledge/instance"),
            "layers": {
                "project": {"roots": [str(ROOT / ".agents/knowledge")]},
                "candidate": {"root": str(ROOT / ".vaws-local/knowledge/candidate")},
            },
        })
    result = configure(config, repository=args.repository, read_only=args.read_only)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
