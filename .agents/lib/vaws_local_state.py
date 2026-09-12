#!/usr/bin/env python3
"""Local untracked state helpers for vllm-ascend-workspace.

Machine inventory and native attachments share the primary Git worktree.
Business result directories remain local to the current worktree.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

STATE_DIRNAME = ".vaws-local"
INVENTORY_FILENAME = "machine-inventory.json"

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / STATE_DIRNAME


def shared_workspace_root(repo_root: Path = ROOT) -> Path:
    """Return the primary worktree that owns cross-worktree machine inventory."""
    repo_root = repo_root.expanduser().resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return repo_root
    if result.returncode != 0 or not result.stdout.strip():
        return repo_root
    common_dir = Path(result.stdout.strip()).expanduser()
    if not common_dir.is_absolute():
        common_dir = repo_root / common_dir
    common_dir = common_dir.resolve()
    if common_dir.name.lower() == ".git" and common_dir.is_dir():
        return common_dir.parent
    return repo_root


def shared_inventory_path(repo_root: Path = ROOT) -> Path:
    return shared_workspace_root(repo_root) / STATE_DIRNAME / INVENTORY_FILENAME


def agent_sessions_root(repo_root: Path = ROOT) -> Path:
    """Native attachments share task identity across linked scaffold worktrees.

    This is a local identity registry, never a second resource allocator.
    Independent clones join a task only through an explicit context receipt.
    """
    return shared_workspace_root(repo_root) / STATE_DIRNAME / "agent-sessions"


class WorkspaceStateError(RuntimeError):
    """Raised for deterministic user-facing local-state failures."""


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def ensure_state_dir(path: Path = STATE_DIR) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


SAFE_RUN_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_run_token(value: str, *, fallback: str = "run", max_len: int = 80) -> str:
    """Sanitize an arbitrary tag into a path-safe run token.

    Over-long tokens are truncated with a stable uuid5 digest suffix so
    distinct inputs stay distinguishable.
    """
    token = SAFE_RUN_TOKEN_RE.sub("-", value.strip()).strip(".-_")
    if not token:
        token = fallback
    if len(token) <= max_len:
        return token
    digest = uuid.uuid5(uuid.NAMESPACE_URL, token).hex[:8]
    keep = max(1, max_len - len(digest) - 1)
    return f"{token[:keep].rstrip('.-_')}-{digest}"


def allocate_run_dir(base_dir: Path, tag: str = "", *, attempts: int = 10) -> Path:
    """Allocate a unique ``<base_dir>/<utc-ts>_<safe_tag>`` directory.

    The first attempt uses the plain ``<ts>_<tag>`` name; collisions retry
    with a short random suffix. Raises after ``attempts`` collisions.
    """
    base_dir = Path(base_dir)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    tag_token = safe_run_token(tag) if tag else ""
    stem = f"{ts}_{tag_token}" if tag_token else ts
    for attempt in range(attempts):
        name = stem if attempt == 0 else f"{stem}_{uuid.uuid4().hex[:8]}"
        run_dir = base_dir / name
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            return run_dir
        except FileExistsError:
            continue
    raise WorkspaceStateError(f"failed to allocate a unique run directory under {base_dir}")
