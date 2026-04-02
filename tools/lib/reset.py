from __future__ import annotations

import json
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .config import RepoPaths
from .remote import CleanupResult, RemoteError, cleanup_runtime, list_managed_server_names, resolve_server_context
from .runtime import read_state, write_state

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


def _load_reset_request(paths: RepoPaths) -> Dict[str, Any]:
    request_path = paths.reset_request_file
    if not request_path.is_file():
        raise RuntimeError("missing pending reset request: run `vaws reset prepare` first")

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid pending reset request: .workspace.local/reset-request.json") from exc

    if not isinstance(request, dict):
        raise RuntimeError("invalid pending reset request: .workspace.local/reset-request.json")
    return request


def _current_server_from_state(paths: RepoPaths) -> Optional[str]:
    state = read_state(paths)
    current_server = state.get("current_server")
    if isinstance(current_server, str) and current_server.strip():
        return current_server.strip()
    return None


def _print_prepare_summary(confirmation_id: str) -> None:
    print(
        "reset prepare: this request records approval to wipe local workspace identity and managed-server runtime"
    )
    print(
        "reset prepare: reset execute will perform managed-server cleanup before clearing local overlay state"
    )
    print(
        "reset prepare: local overlay state includes .workspace.local/state.json, .workspace.local/sessions/, .workspace.local/servers.yaml, .workspace.local/auth.yaml, .workspace.local/repos.yaml, and any retired residue such as .workspace.local/targets.yaml"
    )
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
        approved_server = _current_server_from_state(paths)
        if approved_server:
            request["approved_server"] = approved_server
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
    if paths.local_sessions_dir.exists():
        if not paths.local_sessions_dir.is_dir():
            raise RuntimeError("failed to clean local workspace: .workspace.local/sessions is not a directory")
        shutil.rmtree(paths.local_sessions_dir)
    if paths.local_targets_file.exists():
        if paths.local_targets_file.is_dir():
            raise RuntimeError("failed to clean local workspace: .workspace.local/targets.yaml is not a file")
        paths.local_targets_file.unlink()
    paths.local_servers_file.write_text(CANONICAL_OVERLAY_DEFAULTS["servers"], encoding="utf-8")
    paths.local_auth_file.write_text(CANONICAL_OVERLAY_DEFAULTS["auth"], encoding="utf-8")
    paths.local_repos_file.write_text(CANONICAL_OVERLAY_DEFAULTS["repos"], encoding="utf-8")


def _run_known_hosts_cleanup(host: str, port: int) -> None:
    targets = (host, f"[{host}]:{port}", f"{host}:{port}")
    for target in targets:
        subprocess.run(
            ["ssh-keygen", "-R", target],
            check=False,
            capture_output=True,
            text=True,
        )


def cleanup_server_runtime(paths: RepoPaths, server_name: str) -> CleanupResult:
    context = resolve_server_context(paths, server_name)
    return cleanup_runtime(context)


def _cleanup_remote_state(paths: RepoPaths) -> List[CleanupResult]:
    results: List[CleanupResult] = []
    for server_name in list_managed_server_names(paths):
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


def _load_overlay_mapping(path: Path) -> Dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


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


def _capture_repo_topology(paths: RepoPaths) -> Dict[str, Dict[str, str]]:
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
        raise RuntimeError(
            f"failed to configure remote '{remote_name}' for {repo_path.name}: {stderr}"
        )


def _restore_public_repo_remotes(paths: RepoPaths, topology: Dict[str, Dict[str, str]]) -> None:
    repo_targets = {
        "workspace": paths.root,
        "vllm": paths.root / "vllm",
        "vllm-ascend": paths.root / "vllm-ascend",
    }
    for repo_name, repo_path in repo_targets.items():
        payload = topology.get(repo_name)
        if not isinstance(payload, dict):
            raise RuntimeError(f"missing captured repo topology for {repo_name}")
        origin_url = payload.get("origin_url")
        upstream_url = payload.get("upstream_url")
        if not isinstance(origin_url, str) or not origin_url.strip():
            raise RuntimeError(f"missing captured origin for {repo_name}")
        if not isinstance(upstream_url, str) or not upstream_url.strip():
            raise RuntimeError(f"missing captured upstream for {repo_name}")
        _ensure_git_remote(repo_path, "origin", origin_url.strip())
        _ensure_git_remote(repo_path, "upstream", upstream_url.strip())


def _cleanup_known_hosts_entries(paths: RepoPaths) -> None:
    seen: set[tuple[str, int]] = set()
    servers_config = _load_overlay_mapping(paths.local_servers_file)
    servers = servers_config.get("servers")
    if not isinstance(servers, dict):
        return
    for server in servers.values():
        if not isinstance(server, dict):
            continue
        host = server.get("host")
        runtime = server.get("runtime")
        ssh_port = runtime.get("ssh_port") if isinstance(runtime, dict) else None
        if not isinstance(host, str) or not host.strip():
            continue
        if isinstance(ssh_port, bool) or not isinstance(ssh_port, int):
            continue
        target = (host.strip(), ssh_port)
        if target in seen:
            continue
        seen.add(target)
        _run_known_hosts_cleanup(*target)


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

    if request.get("status") != "pending":
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
        remote_results = _cleanup_remote_state(paths)
    except (RemoteError, RuntimeError) as exc:
        print(f"reset execute: failed to clean remote runtime: {exc}")
        return 1
    for result in remote_results:
        detail = f" ({result.detail})" if result.detail else ""
        print(f"reset execute: server {result.server_name}: {result.status}{detail}")

    try:
        repo_topology = _capture_repo_topology(paths)
    except RuntimeError as exc:
        print(f"reset execute: failed to capture repo topology: {exc}")
        return 1

    _cleanup_known_hosts_entries(paths)

    try:
        _cleanup_local_state(paths)
    except (OSError, RuntimeError) as exc:
        print(f"reset execute: failed to clean local workspace: {exc}")
        return 1

    try:
        _restore_public_repo_remotes(paths, repo_topology)
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
