#!/usr/bin/env python3
"""Locate the vaws-knowledge conformance kit and run it against the client adapter.

The engine is the installed ``vaws-knowledge`` package. Vectors ship with that
package. An explicit ``VAWS_KNOWLEDGE_KIT_ROOT`` (or
``.vaws-local/knowledge-kit-root``) still overrides the package so a local
clone can be used. The kit checkout is not SHA-locked.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

KIT_ROOT_ENV = "VAWS_KNOWLEDGE_KIT_ROOT"
LOCAL_KIT_FILE = ".vaws-local/knowledge-kit-root"
EXPECTED_VECTOR_COUNT = 19


class KitUnconfigured(Exception):
    """No kit root was provided and the installed package has no vectors."""


class KitInvalid(Exception):
    """A configured kit root is missing or incomplete."""


def packaged_kit_root() -> Path | None:
    try:
        import vaws_knowledge.conformance as conformance
    except ImportError:
        return None
    root = Path(conformance.__file__).resolve().parent
    if (root / "runner.py").is_file() and (root / "vectors").is_dir():
        return root
    return None


def _vector_count(root: Path) -> int:
    vectors_dir = root / "vectors"
    if not vectors_dir.is_dir():
        # checkout layout: conformance/vectors
        vectors_dir = root / "conformance" / "vectors"
    if not vectors_dir.is_dir():
        return 0
    return len(list(vectors_dir.glob("*.yaml")))


def _runner_path(root: Path) -> Path:
    packaged = root / "runner.py"
    if packaged.is_file():
        return packaged
    return root / "conformance" / "runner.py"


def _vectors_dir(root: Path) -> Path:
    packaged = root / "vectors"
    if packaged.is_dir():
        return packaged
    return root / "conformance" / "vectors"


def _gate_vectors_dir(root: Path) -> Path:
    packaged = root / "gate_vectors"
    if packaged.is_dir():
        return packaged
    return root / "conformance" / "gate_vectors"


def resolve_kit_root(
    *,
    repo_root: Path = REPO_ROOT,
    environ: Mapping[str, str] | None = None,
    read_local_file: bool = True,
) -> Path:
    """Return an explicit kit checkout, or the installed package kit."""
    mapping = os.environ if environ is None else environ
    raw = str(mapping.get(KIT_ROOT_ENV, "")).strip()
    if not raw and read_local_file:
        local = repo_root / LOCAL_KIT_FILE
        if local.is_file():
            raw = local.read_text(encoding="utf-8").strip()
    if raw:
        root = Path(raw)
        if not root.is_dir():
            raise KitInvalid(f"{KIT_ROOT_ENV} is {raw!r} but that path does not exist")
        runner = _runner_path(root)
        if not runner.is_file():
            raise KitInvalid(f"kit root {root} is missing conformance/runner.py")
        count = _vector_count(root)
        if count != EXPECTED_VECTOR_COUNT:
            raise KitInvalid(
                f"kit root {root} has {count} hash vectors, expected {EXPECTED_VECTOR_COUNT}"
            )
        return root.resolve()
    packaged = packaged_kit_root()
    if packaged is None:
        raise KitUnconfigured(
            f"{KIT_ROOT_ENV} is not set and the vaws-knowledge package has no "
            "conformance vectors; clone vllm-ascend-workspace/vaws-knowledge "
            "and pass --from <clone> or set the env, or run `uv sync`"
        )
    return packaged


def run_client_kit(
    kit_root: Path,
    *,
    repo_root: Path = REPO_ROOT,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    runner = _runner_path(kit_root)
    python = sys.executable
    adapter = repo_root / ".agents" / "tests" / "knowledge_client_adapter.py"

    def command(operation: str) -> str:
        return shlex.join([python, str(adapter), operation])

    return subprocess.run(
        [
            python,
            str(runner),
            "--vectors",
            str(_vectors_dir(kit_root)),
            "--gate-vectors",
            str(_gate_vectors_dir(kit_root)),
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
