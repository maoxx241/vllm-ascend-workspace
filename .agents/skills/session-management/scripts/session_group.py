#!/usr/bin/env python3
"""Create, inspect, and tear down groups of existing VAWS sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_session_state import (  # noqa: E402
    SessionStateError,
    load_session_lookup,
    require_session_id,
)

SCHEMA_VERSION = 1
GROUP_SUBDIR = Path(".vaws-local/sessions/groups")


class SessionGroupError(ValueError):
    """Raised when group membership or lifecycle state is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def group_file(group_id: str, repo_root: Path = ROOT) -> Path:
    return repo_root / GROUP_SUBDIR / require_session_id(group_id) / "group.json"


def parse_members(values: Sequence[str]) -> list[dict[str, str]]:
    members: list[dict[str, str]] = []
    names: set[str] = set()
    sessions: set[str] = set()
    for value in values:
        name, separator, session_id = value.partition("=")
        if not separator or not name.strip() or not session_id.strip():
            raise SessionGroupError(f"member must use NAME=SESSION_ID, got: {value}")
        normalized_name = require_session_id(name.strip())
        normalized_session = require_session_id(session_id.strip())
        if normalized_name in names:
            raise SessionGroupError(f"member name is duplicated: {normalized_name}")
        if normalized_session in sessions:
            raise SessionGroupError(
                f"session is assigned more than once: {normalized_session}"
            )
        names.add(normalized_name)
        sessions.add(normalized_session)
        members.append({"name": normalized_name, "session_id": normalized_session})
    if len(members) < 2:
        raise SessionGroupError("a session group requires at least two members")
    return members


def _git_bytes(
    repo: Path,
    *args: str,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode not in allowed_returncodes:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise SessionGroupError(
            message or f"git {' '.join(args)} failed in {repo}"
        )
    return result.stdout


def _git_text(repo: Path, *args: str) -> str:
    return (
        _git_bytes(repo, *args)
        .decode("utf-8", errors="surrogateescape")
        .strip()
    )


def _digest_value(digest: Any, label: bytes, value: bytes) -> None:
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _submodule_paths(repo: Path) -> list[Path]:
    gitmodules = repo / ".gitmodules"
    if not gitmodules.is_file():
        return []
    output = _git_bytes(
        repo,
        "config",
        "--file",
        str(gitmodules),
        "--get-regexp",
        r"^submodule\..*\.path$",
        allowed_returncodes=(0, 1),
    )
    paths: list[Path] = []
    for raw_line in output.splitlines():
        _key, separator, raw_path = raw_line.partition(b" ")
        if not separator or not raw_path:
            continue
        relative = Path(os.fsdecode(raw_path))
        child = repo / relative
        if child.is_dir() and (child / ".git").exists():
            paths.append(relative)
    return sorted(paths, key=lambda item: os.fsencode(item.as_posix()))


def _dirty_worktree_digest(repo: Path) -> str:
    """Hash executable source state not represented by HEAD or gitlinks."""
    digest = hashlib.sha256()
    tracked_diff = _git_bytes(
        repo,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--submodule=short",
        "HEAD",
        "--",
    )
    _digest_value(digest, b"tracked-diff", tracked_diff)

    untracked = _git_bytes(
        repo,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    for raw_path in sorted(item for item in untracked.split(b"\0") if item):
        path = repo / Path(os.fsdecode(raw_path))
        metadata = path.lstat()
        _digest_value(digest, b"untracked-path", raw_path)
        _digest_value(
            digest, b"untracked-mode", str(metadata.st_mode).encode("ascii")
        )
        if path.is_symlink():
            _digest_value(
                digest, b"untracked-symlink", os.fsencode(os.readlink(path))
            )
            continue
        if not path.is_file():
            _digest_value(digest, b"untracked-special", b"")
            continue
        file_digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                file_digest.update(chunk)
        _digest_value(digest, b"untracked-content", file_digest.digest())

    for relative in _submodule_paths(repo):
        child = repo / relative
        child_status = _git_text(
            child, "status", "--porcelain=v1", "--untracked-files=normal"
        )
        if not child_status:
            continue
        _digest_value(digest, b"submodule-path", os.fsencode(relative.as_posix()))
        _digest_value(
            digest,
            b"submodule-head",
            _git_text(child, "rev-parse", "HEAD").encode("ascii"),
        )
        _digest_value(
            digest,
            b"submodule-dirty",
            bytes.fromhex(_dirty_worktree_digest(child)),
        )
    return digest.hexdigest()


def workspace_snapshot(session: Mapping[str, Any]) -> dict[str, Any]:
    worktree = Path(session["local"]["worktree_root"])
    if not worktree.is_dir():
        raise SessionGroupError(f"session worktree does not exist: {worktree}")

    status = _git_text(
        worktree, "status", "--porcelain=v1", "--untracked-files=normal"
    )
    dirty = bool(status)
    return {
        "workspace_head": _git_text(worktree, "rev-parse", "HEAD"),
        "submodules": _git_text(
            worktree, "submodule", "status", "--recursive"
        ).splitlines(),
        "dirty": dirty,
        "dirty_digest": _dirty_worktree_digest(worktree) if dirty else None,
    }


def _group_snapshot_key(snapshot: Mapping[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_group(
    *,
    repo_root: Path,
    group_id: str,
    member_specs: Sequence[str],
    startup_order: Sequence[str] | None = None,
    snapshot_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]] = workspace_snapshot,
    created_at: str | None = None,
) -> dict[str, Any]:
    gid = require_session_id(group_id)
    path = group_file(gid, repo_root)
    if path.exists():
        raise SessionGroupError(f"session group already exists: {gid}")
    members = parse_members(member_specs)
    member_names = [member["name"] for member in members]
    order = list(startup_order or member_names)
    if len(order) != len(set(order)) or set(order) != set(member_names):
        raise SessionGroupError(
            "startup order must contain every member name exactly once"
        )
    snapshots: dict[str, Mapping[str, Any]] = {}
    resolved: list[dict[str, Any]] = []
    for member in members:
        lookup = load_session_lookup(
            session_id=member["session_id"],
            repo_root=repo_root,
        )
        session = lookup.session
        if session.get("status") != "ready":
            raise SessionGroupError(
                f"session {member['session_id']} must be ready, got {session.get('status')}"
            )
        snapshot = dict(snapshot_resolver(session))
        snapshots[member["name"]] = snapshot
        resolved.append(
            {
                **member,
                "session_file": str(lookup.session_file),
                "base_machine": session["base_machine"],
                "container": session["remote"]["container"]["name"],
                "leased_devices": session.get("leases", {}).get("npu_devices", []),
                "snapshot": snapshot,
            }
        )
    snapshot_keys = {_group_snapshot_key(snapshot) for snapshot in snapshots.values()}
    if len(snapshot_keys) != 1:
        raise SessionGroupError(
            "all group members must use the same workspace and submodule snapshot"
        )
    timestamp = created_at or utc_now()
    group = {
        "schema_version": SCHEMA_VERSION,
        "group_id": gid,
        "status": "ready",
        "members": resolved,
        "startup_order": order,
        "shutdown_order": list(reversed(order)),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _atomic_write(path, group)
    return group


def load_group(group_id: str, repo_root: Path = ROOT) -> dict[str, Any]:
    path = group_file(group_id, repo_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SessionGroupError(f"session group does not exist: {group_id}") from exc
    except json.JSONDecodeError as exc:
        raise SessionGroupError(f"invalid group state {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise SessionGroupError(f"unsupported session group schema: {path}")
    return payload


def inspect_group(
    *,
    repo_root: Path,
    group_id: str,
    snapshot_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]] = workspace_snapshot,
) -> dict[str, Any]:
    group = load_group(group_id, repo_root)
    members: list[dict[str, Any]] = []
    snapshots: set[str] = set()
    for member in group["members"]:
        try:
            lookup = load_session_lookup(
                session_id=member["session_id"],
                repo_root=repo_root,
            )
            session = lookup.session
            snapshot = dict(snapshot_resolver(session))
            snapshots.add(_group_snapshot_key(snapshot))
            members.append(
                {
                    "name": member["name"],
                    "session_id": member["session_id"],
                    "status": session.get("status"),
                    "snapshot_matches_created": snapshot == member["snapshot"],
                    "snapshot": snapshot,
                }
            )
        except (SessionStateError, SessionGroupError) as exc:
            members.append(
                {
                    "name": member["name"],
                    "session_id": member["session_id"],
                    "status": "missing",
                    "error": str(exc),
                    "snapshot_matches_created": False,
                }
            )
    ready = all(
        row["status"] == "ready" and row["snapshot_matches_created"] for row in members
    )
    same_snapshot = len(snapshots) == 1 and len(snapshots) == len(
        {row.get("snapshot") and _group_snapshot_key(row["snapshot"]) for row in members}
        - {None}
    )
    status = "ready" if ready and same_snapshot else "needs_repair"
    return {
        "status": status,
        "group_id": group["group_id"],
        "startup_order": group["startup_order"],
        "shutdown_order": group["shutdown_order"],
        "same_snapshot": same_snapshot,
        "members": members,
        "group_file": str(group_file(group_id, repo_root)),
    }


def list_groups(repo_root: Path = ROOT) -> dict[str, Any]:
    root = repo_root / GROUP_SUBDIR
    groups: list[dict[str, Any]] = []
    if root.is_dir():
        for path in sorted(root.glob("*/group.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                groups.append(
                    {
                        "group_id": payload.get("group_id"),
                        "status": payload.get("status"),
                        "member_count": len(payload.get("members", [])),
                        "group_file": str(path),
                    }
                )
            except (OSError, json.JSONDecodeError):
                groups.append({"status": "invalid", "group_file": str(path)})
    return {"status": "ok", "groups": groups, "count": len(groups)}


def teardown_group(
    *,
    repo_root: Path,
    group_id: str,
    remove_containers: bool,
    remove_worktrees: bool,
    release_leases: bool,
    force: bool,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    updated_at: str | None = None,
) -> dict[str, Any]:
    group = load_group(group_id, repo_root)
    by_name = {member["name"]: member for member in group["members"]}
    results: list[dict[str, Any]] = []
    script = (
        repo_root
        / ".agents"
        / "skills"
        / "session-management"
        / "scripts"
        / "session_remove.py"
    )
    for name in group["shutdown_order"]:
        member = by_name[name]
        command = [
            sys.executable,
            str(script),
            "--session-id",
            member["session_id"],
        ]
        if remove_containers:
            command.append("--remove-container")
        if remove_worktrees:
            command.append("--remove-worktree")
        if release_leases:
            command.append("--release-leases")
        if force:
            command.append("--force")
        completed = runner(
            command,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
        except json.JSONDecodeError:
            payload = {"stdout_tail": completed.stdout[-500:]}
        results.append(
            {
                "name": name,
                "session_id": member["session_id"],
                "returncode": completed.returncode,
                "result": payload,
                "stderr_tail": completed.stderr[-500:],
            }
        )
    success = all(row["returncode"] == 0 for row in results)
    group["status"] = "removed" if success else "needs_repair"
    group["updated_at"] = updated_at or utc_now()
    group["teardown"] = results
    _atomic_write(group_file(group_id, repo_root), group)
    return {
        "status": group["status"],
        "group_id": group["group_id"],
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="action", required=True)
    create_parser = subparsers.add_parser("create", help="bind ready sessions")
    create_parser.add_argument("--group-id", required=True)
    create_parser.add_argument(
        "--member", action="append", required=True, metavar="NAME=SESSION_ID"
    )
    create_parser.add_argument(
        "--startup-order",
        help="comma-separated member names; defaults to --member order",
    )
    status_parser = subparsers.add_parser("status", help="inspect one group")
    status_parser.add_argument("--group-id", required=True)
    subparsers.add_parser("list", help="list groups")
    remove_parser = subparsers.add_parser(
        "teardown", help="stop and optionally remove group members"
    )
    remove_parser.add_argument("--group-id", required=True)
    remove_parser.add_argument("--remove-containers", action="store_true")
    remove_parser.add_argument("--remove-worktrees", action="store_true")
    remove_parser.add_argument("--release-leases", action="store_true")
    remove_parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "create":
            order = args.startup_order.split(",") if args.startup_order else None
            payload = create_group(
                repo_root=ROOT,
                group_id=args.group_id,
                member_specs=args.member,
                startup_order=order,
            )
        elif args.action == "status":
            payload = inspect_group(repo_root=ROOT, group_id=args.group_id)
        elif args.action == "list":
            payload = list_groups(ROOT)
        else:
            payload = teardown_group(
                repo_root=ROOT,
                group_id=args.group_id,
                remove_containers=args.remove_containers,
                remove_worktrees=args.remove_worktrees,
                release_leases=args.release_leases,
                force=args.force,
            )
    except (SessionGroupError, SessionStateError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
