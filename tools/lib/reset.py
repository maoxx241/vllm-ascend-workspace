from __future__ import annotations

import json
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bootstrap import COMMUNITY_UPSTREAM_URLS
from .config import RepoPaths
from .remote import (
    CleanupResult,
    RemoteError,
    cleanup_runtime,
    list_managed_server_names,
    resolve_server_context,
    resolve_target_context,
)
from .runtime import read_state, write_state

CONFIRMATION_PHRASE = "I authorize wiping local workspace identity and remote runtime"


def _ensure_overlay(paths: RepoPaths) -> None:
    if paths.local_overlay.exists() and not paths.local_overlay.is_dir():
        raise RuntimeError("invalid local overlay: .workspace.local/ exists but is not a directory")
    paths.local_overlay.mkdir(parents=True, exist_ok=True)


def _load_reset_request(paths: RepoPaths) -> Dict[str, Any]:
    request_path = paths.reset_request_file
    if not request_path.is_file():
        raise RuntimeError("missing pending reset request: run `vaws reset --prepare` first")

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid pending reset request: .workspace.local/reset-request.json") from exc

    if not isinstance(request, dict):
        raise RuntimeError("invalid pending reset request: .workspace.local/reset-request.json")
    return request


def _write_reset_request(paths: RepoPaths, confirmation_id: str) -> None:
    request = {
        "confirmation_id": confirmation_id,
        "status": "pending",
        "confirmation_phrase": CONFIRMATION_PHRASE,
    }
    paths.reset_request_file.write_text(
        json.dumps(request, indent=2) + "\n",
        encoding="utf-8",
    )


def _current_target_from_state(paths: RepoPaths) -> Optional[str]:
    state = read_state(paths)
    current_target = state.get("current_target")
    if isinstance(current_target, str) and current_target.strip():
        return current_target.strip()
    return None


def _print_prepare_summary(confirmation_id: str) -> None:
    print("reset prepare: this request records approval to wipe local workspace identity and remote runtime context")
    print("reset prepare: reset execute will clean the active remote runtime first, then clear local overlay state")
    print("reset prepare: local overlay state includes .workspace.local/state.json, .workspace.local/sessions/, .workspace.local/targets.yaml, .workspace.local/auth.yaml, and .workspace.local/repos.yaml")
    print(f"reset prepare: confirmation id: {confirmation_id}")
    print(f"reset prepare: confirm with: {CONFIRMATION_PHRASE}")


def prepare_reset(paths: RepoPaths) -> int:
    try:
        _ensure_overlay(paths)
        confirmation_id = f"reset-{secrets.token_urlsafe(16)}"
        request = {
            "confirmation_id": confirmation_id,
            "status": "pending",
            "confirmation_phrase": CONFIRMATION_PHRASE,
        }
        approved_target = _current_target_from_state(paths)
        if approved_target:
            request["approved_target"] = approved_target
        paths.reset_request_file.write_text(
            json.dumps(request, indent=2) + "\n",
            encoding="utf-8",
        )
    except RuntimeError as exc:
        print(str(exc))
        return 1
    except OSError as exc:
        print(f"reset prepare: failed to write reset request: {exc}")
        return 1

    _print_prepare_summary(confirmation_id)
    return 0


def _cleanup_local_state(paths: RepoPaths) -> None:
    write_state(paths, {})
    sessions_path = paths.local_sessions_dir
    if sessions_path.exists():
        if not sessions_path.is_dir():
            raise RuntimeError("failed to clean local workspace: .workspace.local/sessions is not a directory")
        shutil.rmtree(sessions_path)
    paths.local_targets_file.write_text("", encoding="utf-8")
    paths.local_auth_file.write_text("", encoding="utf-8")
    paths.local_repos_file.write_text("", encoding="utf-8")


def cleanup_server_runtime(paths: RepoPaths, server_name: str) -> CleanupResult:
    try:
        context = resolve_server_context(paths, server_name)
    except RemoteError:
        context = resolve_target_context(paths, server_name)
    return cleanup_runtime(context)


def _cleanup_remote_state(request: Dict[str, Any], paths: RepoPaths) -> List[CleanupResult]:
    server_names = list_managed_server_names(paths)
    if not server_names:
        approved_target = request.get("approved_target")
        if isinstance(approved_target, str) and approved_target.strip():
            server_names = [approved_target.strip()]

    results: List[CleanupResult] = []
    for server_name in server_names:
        try:
            results.append(cleanup_server_runtime(paths, server_name))
        except (RemoteError, RuntimeError) as exc:
            results.append(
                CleanupResult(
                    server_name=server_name,
                    status="cleanup_failed",
                    detail=str(exc),
                )
            )
    return results


def _run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
    )


def _ensure_git_remote(repo_path: Path, remote_name: str, url: str) -> None:
    if not (repo_path / ".git").exists():
        raise RuntimeError(f"invalid workspace repo: {repo_path}")

    probe = _run_git(repo_path, "remote", "get-url", remote_name)
    if probe.returncode == 0:
        update = _run_git(repo_path, "remote", "set-url", remote_name, url)
    else:
        update = _run_git(repo_path, "remote", "add", remote_name, url)

    if update.returncode != 0:
        stderr = update.stderr.strip() or update.stdout.strip()
        raise RuntimeError(
            f"failed to configure remote '{remote_name}' for {repo_path.name}: {stderr}"
        )


def _restore_public_repo_remotes(paths: RepoPaths) -> None:
    repo_targets = {
        "vllm": paths.root / "vllm",
        "vllm-ascend": paths.root / "vllm-ascend",
    }

    for repo_name, repo_path in repo_targets.items():
        url = COMMUNITY_UPSTREAM_URLS[repo_name]
        _ensure_git_remote(repo_path, "origin", url)
        _ensure_git_remote(repo_path, "upstream", url)


def execute_reset(
    paths: RepoPaths,
    confirmation_id: Optional[str],
    confirm: Optional[str],
) -> int:
    if not isinstance(confirmation_id, str) or not confirmation_id.strip():
        print("reset execute: missing confirmation id; provide --confirmation-id")
        return 1
    if not isinstance(confirm, str) or not confirm.strip():
        print("reset execute: missing confirmation phrase; provide --confirm")
        return 1

    confirmation_id = confirmation_id.strip()

    try:
        request = _load_reset_request(paths)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    status = request.get("status")
    if status != "pending":
        print("reset execute: reset request status must be pending")
        return 1

    pending_confirmation_id = request.get("confirmation_id")
    if not isinstance(pending_confirmation_id, str) or not pending_confirmation_id.strip():
        print("reset execute: invalid pending reset request: missing confirmation id")
        return 1
    if pending_confirmation_id.strip() != confirmation_id:
        print("reset execute: invalid confirmation id; prepare a new reset request and try again")
        return 1
    if confirm != CONFIRMATION_PHRASE:
        print("reset execute: confirmation phrase must exactly match the approved phrase")
        return 1

    remote_results = _cleanup_remote_state(request, paths)
    for result in remote_results:
        detail = f" ({result.detail})" if result.detail else ""
        print(f"reset execute: server {result.server_name}: {result.status}{detail}")

    try:
        _cleanup_local_state(paths)
    except (OSError, RuntimeError) as exc:
        print(f"reset execute: failed to clean local workspace: {exc}")
        return 1

    try:
        _restore_public_repo_remotes(paths)
    except (OSError, RuntimeError) as exc:
        print(f"reset execute: failed to restore public repo remotes: {exc}")
        return 1

    try:
        if paths.reset_request_file.exists():
            paths.reset_request_file.unlink()
    except OSError as exc:
        print(f"reset execute: failed to clean local workspace: {exc}")
        return 1

    print("reset execute: ok")
    return 0
