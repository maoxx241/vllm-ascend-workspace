from __future__ import annotations

import json
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bootstrap import COMMUNITY_UPSTREAM_URLS
from .config import RepoPaths
from .lifecycle_state import (
    LEGACY_TARGET_HANDOFF_KIND,
    MANAGED_SERVER_HANDOFF_KIND,
    infer_current_target_kind,
)
from .remote import (
    CleanupResult,
    RemoteError,
    can_fallback_to_legacy_target,
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


def _current_target_kind_from_state(paths: RepoPaths) -> Optional[str]:
    state = read_state(paths)
    current_target_kind = state.get("current_target_kind")
    if isinstance(current_target_kind, str) and current_target_kind.strip():
        return current_target_kind.strip()
    current_target = state.get("current_target")
    if not isinstance(current_target, str) or not current_target.strip():
        return None
    runtime = state.get("runtime")
    inferred_kind = infer_current_target_kind(
        paths,
        current_target.strip(),
        runtime if isinstance(runtime, dict) else None,
    )
    if inferred_kind is None:
        raise RuntimeError(
            "missing current target handoff kind: rerun `vaws fleet add <server>` or `vaws target ensure <target>`"
        )
    return inferred_kind
def _cleanup_managed_server_runtime(paths: RepoPaths, server_name: str) -> CleanupResult:
    context = resolve_server_context(paths, server_name)
    return cleanup_runtime(context)


def cleanup_target_runtime(paths: RepoPaths, target_name: str) -> CleanupResult:
    context = resolve_target_context(paths, target_name)
    return cleanup_runtime(context)


def _cleanup_approved_target_runtime(
    paths: RepoPaths,
    target_name: str,
    *,
    handoff_kind: Optional[str],
) -> CleanupResult:
    if handoff_kind == MANAGED_SERVER_HANDOFF_KIND:
        return _cleanup_managed_server_runtime(paths, target_name)
    if handoff_kind == LEGACY_TARGET_HANDOFF_KIND:
        return cleanup_target_runtime(paths, target_name)
    return cleanup_server_runtime(paths, target_name)


def _print_prepare_summary(confirmation_id: str) -> None:
    print(
        "reset prepare: this request records approval to wipe local workspace identity, managed servers, and approved-target cleanup"
    )
    print(
        "reset prepare: reset execute will perform managed-server cleanup first, then approved-target cleanup if needed, before clearing local overlay state"
    )
    print("reset prepare: local overlay state includes .workspace.local/state.json, .workspace.local/sessions/, .workspace.local/servers.yaml, .workspace.local/targets.yaml, .workspace.local/auth.yaml, and .workspace.local/repos.yaml")
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
        approved_target_kind = _current_target_kind_from_state(paths)
        if approved_target:
            request["approved_target"] = approved_target
        if approved_target_kind:
            request["approved_target_kind"] = approved_target_kind
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
    paths.local_servers_file.write_text("version: 1\nservers: {}\n", encoding="utf-8")
    paths.local_auth_file.write_text("", encoding="utf-8")
    paths.local_repos_file.write_text("", encoding="utf-8")


def cleanup_server_runtime(paths: RepoPaths, server_name: str) -> CleanupResult:
    try:
        context = resolve_server_context(paths, server_name)
    except RemoteError as exc:
        if not can_fallback_to_legacy_target(exc):
            raise
        context = resolve_target_context(paths, server_name)
    return cleanup_runtime(context)


def _cleanup_remote_state(request: Dict[str, Any], paths: RepoPaths) -> List[CleanupResult]:
    server_names = list_managed_server_names(paths)
    approved_target = request.get("approved_target")
    approved_target_kind = request.get("approved_target_kind")
    if isinstance(approved_target, str) and approved_target.strip():
        approved_target = approved_target.strip()
    if isinstance(approved_target_kind, str) and approved_target_kind.strip():
        approved_target_kind = approved_target_kind.strip()
    else:
        approved_target_kind = _current_target_kind_from_state(paths)

    results: List[CleanupResult] = []
    cleaned_managed_servers = set()
    for server_name in server_names:
        try:
            results.append(_cleanup_managed_server_runtime(paths, server_name))
            cleaned_managed_servers.add(server_name)
        except (RemoteError, RuntimeError) as exc:
            results.append(
                CleanupResult(
                    server_name=server_name,
                    status="cleanup_failed",
                    detail=str(exc),
                )
            )
    if isinstance(approved_target, str) and approved_target.strip():
        if approved_target_kind is None:
            raise RuntimeError(
                "missing approved target handoff kind: rerun `vaws reset --prepare` first"
            )
        if approved_target_kind == MANAGED_SERVER_HANDOFF_KIND:
            if approved_target not in cleaned_managed_servers:
                try:
                    results.append(_cleanup_managed_server_runtime(paths, approved_target))
                except (RemoteError, RuntimeError) as exc:
                    results.append(
                        CleanupResult(
                            server_name=approved_target,
                            status="cleanup_failed",
                            detail=str(exc),
                        )
                    )
            return results
        if approved_target_kind == LEGACY_TARGET_HANDOFF_KIND:
            try:
                results.append(
                    _cleanup_approved_target_runtime(
                        paths,
                        approved_target,
                        handoff_kind=approved_target_kind,
                    )
                )
            except (RemoteError, RuntimeError) as exc:
                results.append(
                    CleanupResult(
                        server_name=approved_target,
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

    try:
        remote_results = _cleanup_remote_state(request, paths)
    except (RemoteError, RuntimeError) as exc:
        print(f"reset execute: failed to clean remote runtime: {exc}")
        return 1
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
