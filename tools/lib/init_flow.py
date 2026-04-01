from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import yaml

from .bootstrap import (
    BOOTSTRAP_MODE_LOCAL_ONLY,
    BOOTSTRAP_MODE_REMOTE_FIRST,
    BootstrapError,
    DEFAULT_RUNTIME_BOOTSTRAP_MODE,
    DEFAULT_RUNTIME_CONTAINER,
    DEFAULT_RUNTIME_IMAGE,
    DEFAULT_RUNTIME_SSH_PORT,
    DEFAULT_RUNTIME_WORKSPACE_ROOT,
    DEFAULT_SERVER_AUTH_REF,
    DEFAULT_SERVER_PORT,
    _ensure_overlay,
    write_git_auth_ref,
    write_server_auth_ref,
)
from .config import RepoPaths
from .fleet import add_fleet_server
from .foundation import run_foundation
from .git_profile import git_profile
from .lifecycle_state import (
    MANAGED_SERVER_HANDOFF_KIND,
    record_requested_mode,
    record_runtime_handoff,
)
from .secret_boundary import (
    SecretBoundaryError,
    ensure_bootstrap_secret_refs,
    require_pre_staged_env_handle,
)
from .runtime import read_state


@dataclass(frozen=True)
class InitRequest:
    server_host: Optional[str] = None
    server_name: Optional[str] = None
    local_only: bool = False
    server_user: str = "root"
    server_port: int = DEFAULT_SERVER_PORT
    server_auth_mode: str = "ssh-key"
    server_password_env: Optional[str] = None
    server_key_path: Optional[str] = None
    runtime_image: str = DEFAULT_RUNTIME_IMAGE
    runtime_container: str = DEFAULT_RUNTIME_CONTAINER
    runtime_ssh_port: int = DEFAULT_RUNTIME_SSH_PORT
    runtime_workspace_root: str = DEFAULT_RUNTIME_WORKSPACE_ROOT
    runtime_bootstrap_mode: str = DEFAULT_RUNTIME_BOOTSTRAP_MODE
    vllm_origin_url: Optional[str] = None
    vllm_ascend_origin_url: Optional[str] = None
    git_auth_mode: str = "ssh-key"
    git_key_path: Optional[str] = None
    git_token_env: Optional[str] = None


def _optional_non_empty(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def init_request_from_args(args: Any) -> InitRequest:
    return InitRequest(
        server_host=_optional_non_empty(args.server_host),
        server_name=_optional_non_empty(getattr(args, "server_name", None)),
        local_only=bool(getattr(args, "local_only", False)),
        server_user=args.server_user,
        server_port=args.server_port,
        server_auth_mode=args.server_auth_mode,
        server_password_env=args.server_password_env,
        server_key_path=args.server_key_path,
        runtime_image=args.runtime_image,
        runtime_container=args.runtime_container,
        runtime_ssh_port=args.runtime_ssh_port,
        runtime_workspace_root=args.runtime_workspace_root,
        runtime_bootstrap_mode=DEFAULT_RUNTIME_BOOTSTRAP_MODE,
        vllm_origin_url=_optional_non_empty(args.vllm_origin_url),
        vllm_ascend_origin_url=_optional_non_empty(args.vllm_ascend_origin_url),
        git_auth_mode=args.git_auth_mode,
        git_key_path=args.git_key_path,
        git_token_env=args.git_token_env,
    )


def _requested_mode(request: InitRequest) -> str:
    if request.local_only or request.server_host is None:
        return BOOTSTRAP_MODE_LOCAL_ONLY
    return BOOTSTRAP_MODE_REMOTE_FIRST


def _resolved_server_name(request: InitRequest) -> Optional[str]:
    if _requested_mode(request) != BOOTSTRAP_MODE_REMOTE_FIRST:
        return None
    return _optional_non_empty(request.server_name) or request.server_host


def _read_yaml_mapping(path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BootstrapError(f"invalid init state: {path.name}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise BootstrapError(f"invalid init state: {path.name}")
    return loaded


def _server_is_ready(paths: RepoPaths, *, server_name: str, server_host: str) -> bool:
    servers_config = _read_yaml_mapping(paths.local_servers_file)
    servers = servers_config.get("servers")
    if not isinstance(servers, dict):
        return False

    server = servers.get(server_name)
    if not isinstance(server, dict):
        return False
    if server.get("host") != server_host:
        return False

    verification = server.get("verification")
    server_status = server.get("status")
    verification_status = (
        verification.get("status") if isinstance(verification, dict) else None
    )
    if server_status != "ready" or verification_status != "ready":
        return False

    state = read_state(paths)
    server_verifications = state.get("server_verifications")
    if not isinstance(server_verifications, dict):
        return False
    persisted = server_verifications.get(server_name)
    if not isinstance(persisted, dict):
        return False
    return persisted.get("status") == "ready"


def _server_matches_request(
    paths: RepoPaths,
    *,
    server_name: str,
    request: InitRequest,
) -> bool:
    if request.server_host is None:
        return False

    servers_config = _read_yaml_mapping(paths.local_servers_file)
    servers = servers_config.get("servers")
    if not isinstance(servers, dict):
        return False

    server = servers.get(server_name)
    if not isinstance(server, dict):
        return False

    if server.get("host") != request.server_host:
        return False
    if server.get("port") != request.server_port:
        return False
    if server.get("login_user") != request.server_user:
        return False

    runtime = server.get("runtime")
    if not isinstance(runtime, dict):
        return False
    if runtime.get("image_ref") != request.runtime_image:
        return False
    if runtime.get("container_name") != request.runtime_container:
        return False
    if runtime.get("ssh_port") != request.runtime_ssh_port:
        return False
    if runtime.get("workspace_root") != request.runtime_workspace_root:
        return False
    if runtime.get("bootstrap_mode") != request.runtime_bootstrap_mode:
        return False
    return True


def _resolved_ready_server_name(
    paths: RepoPaths,
    *,
    server_name: Optional[str],
    request: InitRequest,
) -> Optional[str]:
    explicit_server_name = _optional_non_empty(server_name)
    if explicit_server_name is not None:
        if _server_is_ready(
            paths,
            server_name=explicit_server_name,
            server_host=request.server_host or "",
        ) and _server_matches_request(
            paths,
            server_name=explicit_server_name,
            request=request,
        ):
            return explicit_server_name
        return None

    servers_config = _read_yaml_mapping(paths.local_servers_file)
    servers = servers_config.get("servers")
    if not isinstance(servers, dict):
        return None

    for existing_name in sorted(servers):
        if not isinstance(existing_name, str) or not existing_name.strip():
            continue
        if _server_is_ready(
            paths,
            server_name=existing_name,
            server_host=request.server_host or "",
        ) and _server_matches_request(
            paths,
            server_name=existing_name,
            request=request,
        ):
            return existing_name
    return None


def _runtime_for_ready_server(paths: RepoPaths, server_name: str) -> Dict[str, Any]:
    state = read_state(paths)
    server_verifications = state.get("server_verifications")
    if isinstance(server_verifications, dict):
        persisted = server_verifications.get(server_name)
        if isinstance(persisted, dict):
            runtime = persisted.get("runtime")
            if isinstance(runtime, dict):
                return dict(runtime)

    servers_config = _read_yaml_mapping(paths.local_servers_file)
    servers = servers_config.get("servers")
    if isinstance(servers, dict):
        server = servers.get(server_name)
        if isinstance(server, dict):
            verification = server.get("verification")
            if isinstance(verification, dict):
                runtime = verification.get("runtime")
                if isinstance(runtime, dict):
                    return dict(runtime)

    raise BootstrapError(f"missing ready runtime handoff for existing server: {server_name}")


def _requested_server_auth_matches(paths: RepoPaths, request: InitRequest) -> bool:
    auth_config = _read_yaml_mapping(paths.local_auth_file)
    ssh_auth = auth_config.get("ssh_auth")
    if not isinstance(ssh_auth, dict):
        return False
    refs = ssh_auth.get("refs")
    if not isinstance(refs, dict):
        return False
    auth_ref = refs.get(DEFAULT_SERVER_AUTH_REF)
    if not isinstance(auth_ref, dict):
        return False
    return (
        auth_ref.get("kind") == request.server_auth_mode
        and auth_ref.get("username") == request.server_user
        and auth_ref.get("password_env") == request.server_password_env
        and auth_ref.get("key_path") == request.server_key_path
    )


def _preserve_requested_git_auth(paths: RepoPaths, request: InitRequest) -> None:
    write_git_auth_ref(
        paths,
        git_auth_mode=request.git_auth_mode,
        git_key_path=request.git_key_path,
        git_token_env=request.git_token_env,
    )


def _ensure_requested_git_auth_secret_refs(request: InitRequest) -> None:
    if request.git_auth_mode == "token":
        require_pre_staged_env_handle(
            request.git_token_env,
            field_label="git token",
        )


def _git_profile_needs_input(paths: RepoPaths) -> bool:
    state = read_state(paths)
    lifecycle = state.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return False
    git_profile_state = lifecycle.get("git_profile")
    if not isinstance(git_profile_state, dict):
        return False
    return git_profile_state.get("status") == "needs_input"


def run_init(paths: RepoPaths, request: InitRequest) -> int:
    try:
        _ensure_overlay(paths)
        requested_mode = _requested_mode(request)
        record_requested_mode(paths, requested_mode)

        if run_foundation(paths) != 0:
            return 1

        git_profile_result = git_profile(
                paths,
                vllm_origin_url=request.vllm_origin_url,
                vllm_ascend_origin_url=request.vllm_ascend_origin_url,
            )
        if git_profile_result != 0:
            if (
                requested_mode == BOOTSTRAP_MODE_LOCAL_ONLY
                and request.vllm_origin_url is None
                and request.vllm_ascend_origin_url is None
                and _git_profile_needs_input(paths)
            ):
                _ensure_requested_git_auth_secret_refs(request)
                _preserve_requested_git_auth(paths, request)
                print("init: ready (local-only)")
                return 0
            return 1

        _ensure_requested_git_auth_secret_refs(request)
        _preserve_requested_git_auth(paths, request)

        if requested_mode == BOOTSTRAP_MODE_LOCAL_ONLY:
            print("init: ready (local-only)")
            return 0

        if request.server_host is None:
            raise BootstrapError("missing server host for remote-first init")
        server_name = _resolved_server_name(request)
        if server_name is None:
            raise BootstrapError("missing server name for remote-first init")

        ready_server_name = _resolved_ready_server_name(
            paths,
            server_name=request.server_name,
            request=request,
        )
        if ready_server_name is not None:
            if not _requested_server_auth_matches(paths, request):
                ensure_bootstrap_secret_refs(
                    server_auth_mode=request.server_auth_mode,
                    server_password_env=request.server_password_env,
                    git_auth_mode=request.git_auth_mode,
                    git_token_env=request.git_token_env,
                )
                write_server_auth_ref(
                    paths,
                    server_auth_mode=request.server_auth_mode,
                    server_user=request.server_user,
                    server_password_env=request.server_password_env,
                    server_key_path=request.server_key_path,
                )
            record_runtime_handoff(
                paths,
                current_target=ready_server_name,
                handoff_kind=MANAGED_SERVER_HANDOFF_KIND,
                runtime=_runtime_for_ready_server(paths, ready_server_name),
            )
            print(f"init: ready (existing {ready_server_name})")
            return 0

        ensure_bootstrap_secret_refs(
            server_auth_mode=request.server_auth_mode,
            server_password_env=request.server_password_env,
            git_auth_mode=request.git_auth_mode,
            git_token_env=request.git_token_env,
        )
        write_server_auth_ref(
            paths,
            server_auth_mode=request.server_auth_mode,
            server_user=request.server_user,
            server_password_env=request.server_password_env,
            server_key_path=request.server_key_path,
        )
        return add_fleet_server(
            paths,
            server_name,
            request.server_host,
            ssh_auth_ref=DEFAULT_SERVER_AUTH_REF,
            server_user=request.server_user,
            server_port=request.server_port,
            runtime_image=request.runtime_image,
            runtime_container=request.runtime_container,
            runtime_ssh_port=request.runtime_ssh_port,
            runtime_workspace_root=request.runtime_workspace_root,
            runtime_bootstrap_mode=request.runtime_bootstrap_mode,
        )
    except (BootstrapError, SecretBoundaryError, RuntimeError) as exc:
        print(str(exc))
        return 1
