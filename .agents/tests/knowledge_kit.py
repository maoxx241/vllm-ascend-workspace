#!/usr/bin/env python3
"""Locate the pinned vaws-knowledge kit and run it against the client adapter."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPS_PATH = REPO_ROOT / ".agents" / "deps" / "vaws-knowledge.json"
KIT_ROOT_ENV = "VAWS_KNOWLEDGE_KIT_ROOT"
LOCAL_KIT_FILE = ".vaws-local/knowledge-kit-root"
EXPECTED_VECTOR_COUNT = 17


class KitUnconfigured(Exception):
    """No explicit kit root was provided."""


class KitInvalid(Exception):
    """A configured kit root is missing, incomplete, or the wrong revision."""


def pinned_commit(repo_root: Path = REPO_ROOT) -> str:
    payload = json.loads((repo_root / ".agents" / "deps" / "vaws-knowledge.json").read_text(encoding="utf-8"))
    commit = payload.get("commit")
    if not isinstance(commit, str) or len(commit) != 40:
        raise KitInvalid(f"{DEPS_PATH} does not declare a 40-character commit")
    return commit


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _verify_pinned_git_kit(root: Path, expected: str) -> None:
    """Require a clean Git checkout or worktree of the pinned kit commit.

    ``.git`` may be a directory or a worktree file. Executed runner, vector
    and gate-vector bytes must match the pinned commit; a matching filename
    count is not identity.
    """

    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise KitInvalid(f"kit root {root} is not a Git checkout of {expected}")
    toplevel = _git(root, "rev-parse", "--show-toplevel")
    if toplevel.returncode != 0:
        raise KitInvalid(f"git rev-parse --show-toplevel failed in {root}")
    if Path(toplevel.stdout.strip()).resolve() != root.resolve():
        raise KitInvalid(
            f"kit root {root} is not a Git toplevel of {expected}; "
            "refusing a nested or borrowed repository"
        )
    head = _git(root, "rev-parse", "HEAD")
    if head.returncode != 0 or head.stdout.strip() != expected:
        raise KitInvalid(
            f"kit root {root} is git commit {head.stdout.strip() or 'unknown'}, "
            f"expected {expected}"
        )
    dirty = _git(
        root,
        "diff",
        "--quiet",
        expected,
        "--",
        "conformance/runner.py",
        "conformance/vectors",
        "conformance/gate_vectors",
    )
    if dirty.returncode == 1:
        raise KitInvalid(
            f"kit root {root} executed kit files do not match {expected}"
        )
    if dirty.returncode != 0:
        raise KitInvalid(f"git diff failed in {root}: {dirty.stderr.strip()}")


def resolve_kit_root(
    *,
    repo_root: Path = REPO_ROOT,
    environ: Mapping[str, str] | None = None,
    read_local_file: bool = True,
) -> Path:
    """Return the configured kit root or raise.

    Configuration is explicit: ``VAWS_KNOWLEDGE_KIT_ROOT``, or a one-line path
    in ``.vaws-local/knowledge-kit-root``. Unconfigured is not a pass. A
    configured missing path, non-Git tree, wrong revision, or dirty executed
    runner/vector bytes is a failure, not a skip.
    """

    expected = pinned_commit(repo_root)
    mapping = os.environ if environ is None else environ
    raw = str(mapping.get(KIT_ROOT_ENV, "")).strip()
    if not raw and read_local_file:
        local = repo_root / LOCAL_KIT_FILE
        if local.is_file():
            raw = local.read_text(encoding="utf-8").strip()
    if not raw:
        raise KitUnconfigured(
            f"{KIT_ROOT_ENV} is not set; shared-kit client tests require an explicit "
            f"git checkout of {expected}"
        )
    root = Path(raw)
    if not root.is_dir():
        raise KitInvalid(f"{KIT_ROOT_ENV} is {raw!r} but that path does not exist")
    runner = root / "conformance" / "runner.py"
    if not runner.is_file():
        raise KitInvalid(f"kit root {root} is missing conformance/runner.py")
    vectors_dir = root / "conformance" / "vectors"
    vector_count = len(list(vectors_dir.glob("*.yaml"))) if vectors_dir.is_dir() else 0
    if vector_count != EXPECTED_VECTOR_COUNT:
        raise KitInvalid(
            f"kit root {root} has {vector_count} hash vectors, expected {EXPECTED_VECTOR_COUNT}"
        )
    _verify_pinned_git_kit(root, expected)
    return root.resolve()


def run_client_kit(
    kit_root: Path,
    *,
    repo_root: Path = REPO_ROOT,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    runner = kit_root / "conformance" / "runner.py"
    python = sys.executable
    adapter = repo_root / ".agents" / "tests" / "knowledge_client_adapter.py"

    def command(operation: str) -> str:
        return shlex.join([python, str(adapter), operation])

    return subprocess.run(
        [
            python,
            str(runner),
            "--vectors",
            str(kit_root / "conformance" / "vectors"),
            "--gate-vectors",
            str(kit_root / "conformance" / "gate_vectors"),
            "--hash-cmd",
            command("hash"),
            "--payload-cmd",
            command("payload"),
            "--schema-cmd",
            command("schema"),
            "--redaction-cmd",
            command("redaction"),
            "--export-cmd",
            command("export"),
            "--input-format",
            "entry-json",
            "--gate-format",
            "document-json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(kit_root),
        timeout=timeout,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit-root", type=Path, default=None)
    args = parser.parse_args(argv)
    repo_root = REPO_ROOT
    if args.kit_root is not None:
        kit = resolve_kit_root(
            repo_root=repo_root,
            environ={KIT_ROOT_ENV: str(args.kit_root)},
            read_local_file=False,
        )
    else:
        kit = resolve_kit_root(repo_root=repo_root)
    completed = run_client_kit(kit, repo_root=repo_root)
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
