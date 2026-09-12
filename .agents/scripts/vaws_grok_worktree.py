#!/usr/bin/env python3
"""Prepare a Grok directory while native Git worktree creation is completing.

Invoked by the source repository's post-checkout hook in Grok's ordinary Git
worktree mode. No session, polling or shell-command rewriting is involved.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
sys.path.insert(0, str(ROOT / ".agents/scripts"))

from vaws_environment import PIN_ENV, MANAGED_PIN_ENV
from vaws_workspace_update import common_dir, git
from vaws_worktree_setup import prepare_worktree


def creation_target(source: Path, target: Path, previous: str, revision: str,
                    checkout_kind: str, grok_home: Path) -> bool:
    """Only Grok's newly populated linked worktrees qualify for preparation."""
    if checkout_kind != "1" or len(previous) not in (40, 64) or set(previous) != {"0"}:
        return False
    source, target = source.resolve(), target.resolve()
    if source == target or not target.is_relative_to((grok_home / "worktrees").resolve()):
        return False
    shared = common_dir(source).resolve()
    if common_dir(target).resolve() != shared:
        return False
    if Path(git(target, "rev-parse", "--absolute-git-dir")).resolve() == shared:
        return False
    if Path(git(target, "rev-parse", "--show-toplevel")).resolve() != target:
        return False
    return git(target, "rev-parse", "HEAD") == revision


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("previous")
    parser.add_argument("revision")
    parser.add_argument("checkout_kind")
    args = parser.parse_args(argv)
    try:
        target = Path.cwd()
        grok_home = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok"))).expanduser()
        if not creation_target(args.source, target, args.previous, args.revision, args.checkout_kind, grok_home):
            return 0
        os.environ.pop(PIN_ENV, None)
        os.environ.pop(MANAGED_PIN_ENV, None)
        result = prepare_worktree("grok", args.source.resolve(), target.resolve())
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "phase": "grok_worktree_setup", "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
