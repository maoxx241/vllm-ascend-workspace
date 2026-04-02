from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import yaml

from .capability_state import read_capability_state, write_capability_state
from .config import RepoPaths
from .git_auth import ensure_git_auth_ready
from .machine import (
    DEFAULT_RUNTIME_BOOTSTRAP_MODE,
    DEFAULT_RUNTIME_CONTAINER,
    DEFAULT_RUNTIME_IMAGE,
    DEFAULT_RUNTIME_SSH_PORT,
    DEFAULT_RUNTIME_WORKSPACE_ROOT,
    DEFAULT_SERVER_PORT,
    add_machine,
)
from .overlay import ensure_overlay_layout
from .repo_targets import resolve_repo_targets
from .repo_topology import ensure_repo_topology_ready
from .runtime import read_state, update_state
from .secret_boundary import SecretBoundaryError, ensure_bootstrap_secret_refs

DEFAULT_SERVER_AUTH_REF = "default-server-auth"
DEFAULT_GIT_AUTH_REF = "default-github-cli"
REQUEST_MODE_LOCAL_ONLY = "local-only"
REQUEST_MODE_REMOTE_FIRST = "remote-first"


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
    vllm_upstream_tag: Optional[str] = None
    vllm_ascend_upstream_branch: Optional[str] = None
    require_feature_branch: bool = False


def _optional_non_empty(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def init_request_from_args(args: Any) -> InitRequest:
    return InitRequest(
        server_host=_optional_non_empty(getattr(args, "server_host", None)),
        server_name=_optional_non_empty(getattr(args, "server_name", None)),
        local_only=bool(getattr(args, "local_only", False)),
        server_user=getattr(args, "server_user", "root"),
        server_port=getattr(args, "server_port", DEFAULT_SERVER_PORT),
        server_auth_mode=getattr(args, "server_auth_mode", "ssh-key"),
        server_password_env=_optional_non_empty(getattr(args, "server_password_env", None)),
        server_key_path=_optional_non_empty(getattr(args, "server_key_path", None)),
        runtime_image=getattr(args, "runtime_image", DEFAULT_RUNTIME_IMAGE),
        runtime_container=getattr(args, "runtime_container", DEFAULT_RUNTIME_CONTAINER),
        runtime_ssh_port=getattr(args, "runtime_ssh_port", DEFAULT_RUNTIME_SSH_PORT),
        runtime_workspace_root=getattr(args, "runtime_workspace_root", DEFAULT_RUNTIME_WORKSPACE_ROOT),
        runtime_bootstrap_mode=getattr(
            args,
            "runtime_bootstrap_mode",
            DEFAULT_RUNTIME_BOOTSTRAP_MODE,
        ),
        vllm_origin_url=_optional_non_empty(getattr(args, "vllm_origin_url", None)),
        vllm_ascend_origin_url=_optional_non_empty(getattr(args, "vllm_ascend_origin_url", None)),
        vllm_upstream_tag=_optional_non_empty(getattr(args, "vllm_upstream_tag", None)),
        vllm_ascend_upstream_branch=_optional_non_empty(
            getattr(args, "vllm_ascend_upstream_branch", None)
        ),
        require_feature_branch=bool(getattr(args, "require_feature_branch", False)),
    )


def _requested_mode(request: InitRequest) -> str:
    if request.local_only or request.server_host is None:
        return REQUEST_MODE_LOCAL_ONLY
    return REQUEST_MODE_REMOTE_FIRST


def _resolved_server_name(request: InitRequest) -> Optional[str]:
    if _requested_mode(request) != REQUEST_MODE_REMOTE_FIRST:
        return None
    return _optional_non_empty(request.server_name) or request.server_host


def _read_yaml_mapping(path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeError(f"invalid init state: {path.name}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"invalid init state: {path.name}")
    return loaded


def _write_auth_config(paths: RepoPaths, auth_config: Dict[str, Any]) -> None:
    paths.local_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.local_auth_file.write_text(
        yaml.safe_dump(auth_config, sort_keys=False),
        encoding="utf-8",
    )


def _load_auth_config(paths: RepoPaths) -> Dict[str, Any]:
    auth_config = _read_yaml_mapping(paths.local_auth_file)
    auth_config.setdefault("version", 1)
    git_auth = auth_config.setdefault("git_auth", {})
    if not isinstance(git_auth, dict):
        git_auth = {}
        auth_config["git_auth"] = git_auth
    git_auth.setdefault("refs", {})
    ssh_auth = auth_config.setdefault("ssh_auth", {})
    if not isinstance(ssh_auth, dict):
        ssh_auth = {}
        auth_config["ssh_auth"] = ssh_auth
    ssh_auth.setdefault("refs", {})
    return auth_config


def _write_git_auth_provider(paths: RepoPaths) -> None:
    auth_config = _load_auth_config(paths)
    refs = auth_config["git_auth"]["refs"]
    refs[DEFAULT_GIT_AUTH_REF] = {
        "kind": "github-cli",
    }
    _write_auth_config(paths, auth_config)


def _write_server_auth_ref(paths: RepoPaths, request: InitRequest) -> None:
    auth_config = _load_auth_config(paths)
    refs = auth_config["ssh_auth"]["refs"]
    payload: Dict[str, Any] = {
        "kind": request.server_auth_mode,
        "username": request.server_user,
    }
    if request.server_password_env:
        payload["password_env"] = request.server_password_env
    if request.server_key_path:
        payload["key_path"] = request.server_key_path
    refs[DEFAULT_SERVER_AUTH_REF] = payload
    _write_auth_config(paths, auth_config)


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
    if server.get("status") != "ready":
        return False

    state = read_state(paths)
    server_state = state.get("servers", {}).get(server_name) if isinstance(state.get("servers"), dict) else None
    container_access = server_state.get("container_access") if isinstance(server_state, dict) else None
    return isinstance(container_access, dict) and container_access.get("status") == "ready"


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


def _requested_server_auth_matches(paths: RepoPaths, request: InitRequest) -> bool:
    auth_config = _load_auth_config(paths)
    refs = auth_config["ssh_auth"]["refs"]
    auth_ref = refs.get(DEFAULT_SERVER_AUTH_REF)
    if not isinstance(auth_ref, dict):
        return False
    return (
        auth_ref.get("kind") == request.server_auth_mode
        and auth_ref.get("username") == request.server_user
        and auth_ref.get("password_env") == request.server_password_env
        and auth_ref.get("key_path") == request.server_key_path
    )


def _record_repo_targets(paths: RepoPaths, request: InitRequest) -> None:
    targets = resolve_repo_targets(
        paths,
        vllm_upstream_tag=request.vllm_upstream_tag,
        vllm_ascend_upstream_branch=request.vllm_ascend_upstream_branch,
        require_feature_branch=request.require_feature_branch,
    )
    update_state(paths, repo_targets=targets.to_mapping())


def run_init(paths: RepoPaths, request: InitRequest) -> int:
    try:
        ensure_overlay_layout(paths)

        git_auth_result = ensure_git_auth_ready(paths)
        if git_auth_result["status"] != "ready":
            print(f"init: {git_auth_result['status']}: {git_auth_result['detail']}")
            return 1

        topology_result = ensure_repo_topology_ready(paths)
        if topology_result["status"] != "ready":
            print(f"init: {topology_result['status']}: {topology_result['detail']}")
            return 1

        _write_git_auth_provider(paths)
        _record_repo_targets(paths, request)

        if _requested_mode(request) == REQUEST_MODE_LOCAL_ONLY:
            print("init: ready (local-only)")
            return 0

        if request.server_host is None:
            raise RuntimeError("missing server host for remote-first init")
        server_name = _resolved_server_name(request)
        if server_name is None:
            raise RuntimeError("missing server name for remote-first init")

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
                    server_password_scope="workspace-init:first-machine-attach",
                    git_auth_mode="github-cli",
                    git_token_env=None,
                )
                _write_server_auth_ref(paths, request)
            state = read_capability_state(paths)
            state["current_server"] = ready_server_name
            state.pop("current_session", None)
            write_capability_state(paths, state)
            print(f"init: ready (existing {ready_server_name})")
            return 0

        ensure_bootstrap_secret_refs(
            server_auth_mode=request.server_auth_mode,
            server_password_env=request.server_password_env,
            server_password_scope="workspace-init:first-machine-attach",
            git_auth_mode="github-cli",
            git_token_env=None,
        )
        _write_server_auth_ref(paths, request)
        return add_machine(
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
    except (SecretBoundaryError, RuntimeError) as exc:
        print(str(exc))
        return 1
