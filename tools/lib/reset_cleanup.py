from __future__ import annotations

import json
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .agent_contract import validate_tool_result
from .config import RepoPaths
from .machine_registry import list_server_records
from .remote_types import RemoteError
from .runtime_cleanup import cleanup_runtime
from .target_context import resolve_server_context

CONFIRMATION_PHRASE = "I authorize wiping local workspace identity and remote runtime"
CANONICAL_OVERLAY_DEFAULTS = {
    "servers": "version: 1\nservers: {}\n",
    "auth": "version: 1\nssh_auth: {refs: {}}\ngit_auth: {refs: {}}\n",
    "repos": "version: 1\nworkspace: {}\nsubmodules: {}\n",
}


def _ensure_overlay(paths: RepoPaths) -> None:
    if paths.local_overlay.exists() and not paths.local_overlay.is_dir():
        raise RuntimeError("invalid local overlay: .workspace.local/ exists but is not a directory")
    paths.local_overlay.mkdir(parents=True, exist_ok=True)


def _remove_overlay_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink()


def prepare_reset_request(paths: RepoPaths) -> dict[str, object]:
    try:
        _ensure_overlay(paths)
        confirmation_id = f"reset-{secrets.token_urlsafe(16)}"
        request: dict[str, object] = {
            "confirmation_id": confirmation_id,
            "status": "pending",
            "confirmation_phrase": CONFIRMATION_PHRASE,
        }
        registered_servers = sorted(list_server_records(paths))
        if registered_servers:
            request["registered_servers"] = registered_servers
        paths.reset_request_file.write_text(
            json.dumps(request, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, RuntimeError) as exc:
        return validate_tool_result(
            {
                "status": "cleanup_failed",
                "observations": ["failed to record pending reset request"],
                "reason": str(exc),
                "side_effects": [],
                "retryable": True,
                "idempotent": False,
            },
            action_kind="execute",
        )

    return validate_tool_result(
        {
            "status": "ready",
            "observations": ["recorded pending reset request"],
            "side_effects": ["reset request file updated"],
            "payload": request,
            "idempotent": False,
        },
        action_kind="execute",
    )


def cleanup_remote_runtime(paths: RepoPaths, server_name: str) -> dict[str, object]:
    try:
        context = resolve_server_context(paths, server_name)
        result = cleanup_runtime(context)
    except (RemoteError, RuntimeError) as exc:
        return validate_tool_result(
            {
                "status": "cleanup_failed",
                "observations": [f"remote runtime cleanup failed for {server_name}"],
                "reason": str(exc),
                "side_effects": [],
                "retryable": True,
                "idempotent": True,
            },
            action_kind="cleanup",
        )

    if result.status in {"ready", "removed", "already_absent"}:
        return validate_tool_result(
            {
                "status": "ready",
                "observations": [f"remote runtime cleanup completed for {server_name}: {result.status}"],
                "side_effects": ["remote runtime cleanup attempted"],
                "payload": result.to_mapping(),
                "idempotent": True,
            },
            action_kind="cleanup",
        )

    return validate_tool_result(
        {
            "status": "cleanup_failed",
            "observations": [f"remote runtime cleanup failed for {server_name}"],
            "reason": result.detail,
            "side_effects": [],
            "retryable": True,
            "idempotent": True,
            "payload": result.to_mapping(),
        },
        action_kind="cleanup",
    )


def cleanup_overlay(paths: RepoPaths) -> dict[str, object]:
    try:
        _ensure_overlay(paths)
        _remove_overlay_path(paths.local_overlay / "state.json")
        _remove_overlay_path(paths.local_overlay / "targets.yaml")
        _remove_overlay_path(paths.local_overlay / "sessions")
        _remove_overlay_path(paths.local_benchmark_runs_dir)
        _remove_overlay_path(paths.reset_request_file)
        paths.local_servers_file.write_text(CANONICAL_OVERLAY_DEFAULTS["servers"], encoding="utf-8")
        paths.local_auth_file.write_text(CANONICAL_OVERLAY_DEFAULTS["auth"], encoding="utf-8")
        paths.local_repos_file.write_text(CANONICAL_OVERLAY_DEFAULTS["repos"], encoding="utf-8")
    except (OSError, RuntimeError) as exc:
        return validate_tool_result(
            {
                "status": "cleanup_failed",
                "observations": ["failed to reset local overlay files"],
                "reason": str(exc),
                "side_effects": [],
                "retryable": True,
                "idempotent": True,
            },
            action_kind="cleanup",
        )

    return validate_tool_result(
        {
            "status": "ready",
            "observations": ["reset local overlay files to canonical defaults"],
            "side_effects": ["local overlay reset"],
            "idempotent": True,
        },
        action_kind="cleanup",
    )


def cleanup_known_hosts(host: str, port: int) -> dict[str, object]:
    targets = (host, f"[{host}]:{port}", f"{host}:{port}")
    for target in targets:
        subprocess.run(
            ["ssh-keygen", "-R", target],
            check=False,
            capture_output=True,
            text=True,
        )
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"removed known_hosts entries for {host}:{port}"],
            "side_effects": ["known_hosts entries removed"],
            "idempotent": True,
        },
        action_kind="cleanup",
    )


def _run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
    )


def _git_remote_url(repo_path: Path, remote_name: str) -> str:
    result = _run_git(repo_path, "remote", "get-url", remote_name)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"failed to read remote '{remote_name}' for {repo_path.name}: {stderr}")
    remote_url = result.stdout.strip()
    if not remote_url:
        raise RuntimeError(f"failed to read remote '{remote_name}' for {repo_path.name}: empty url")
    return remote_url


def _capture_repo_topology(paths: RepoPaths) -> dict[str, dict[str, str]]:
    return {
        "workspace": {
            "origin_url": _git_remote_url(paths.root, "origin"),
            "upstream_url": _git_remote_url(paths.root, "upstream"),
        },
        "vllm": {
            "origin_url": _git_remote_url(paths.root / "vllm", "origin"),
            "upstream_url": _git_remote_url(paths.root / "vllm", "upstream"),
        },
        "vllm-ascend": {
            "origin_url": _git_remote_url(paths.root / "vllm-ascend", "origin"),
            "upstream_url": _git_remote_url(paths.root / "vllm-ascend", "upstream"),
        },
    }


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
        raise RuntimeError(f"failed to configure remote '{remote_name}' for {repo_path.name}: {stderr}")


def restore_public_remotes(paths: RepoPaths) -> dict[str, object]:
    try:
        topology = _capture_repo_topology(paths)
        repo_targets = {
            "workspace": paths.root,
            "vllm": paths.root / "vllm",
            "vllm-ascend": paths.root / "vllm-ascend",
        }
        for repo_name, repo_path in repo_targets.items():
            payload = topology[repo_name]
            _ensure_git_remote(repo_path, "origin", payload["origin_url"])
            _ensure_git_remote(repo_path, "upstream", payload["upstream_url"])
    except (KeyError, RuntimeError) as exc:
        return validate_tool_result(
            {
                "status": "cleanup_failed",
                "observations": ["failed to restore public remotes"],
                "reason": str(exc),
                "side_effects": [],
                "retryable": True,
                "idempotent": True,
            },
            action_kind="repair",
        )

    return validate_tool_result(
        {
            "status": "ready",
            "observations": ["re-applied public git remotes from live repository topology"],
            "side_effects": ["workspace and submodule remotes updated"],
            "payload": topology,
            "idempotent": True,
        },
        action_kind="repair",
    )
