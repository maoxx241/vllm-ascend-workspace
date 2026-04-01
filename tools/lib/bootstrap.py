from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .config import RepoPaths
from .overlay import ensure_overlay_layout
from .remote import DEFAULT_HOST_WORKSPACE_BASE
from .remote import VerificationCheck, VerificationResult
from .remote import TargetContext
from .runtime import read_state, write_state

COMMUNITY_UPSTREAM_URLS = {
    "vllm": "https://github.com/vllm-project/vllm.git",
    "vllm-ascend": "https://github.com/vllm-project/vllm-ascend.git",
}

DEFAULT_TARGET_NAME = "single-default"
DEFAULT_HOST_NAME = "host-a"
DEFAULT_RUNTIME_IMAGE = "quay.nju.edu.cn/ascend/vllm-ascend:latest"
DEFAULT_RUNTIME_CONTAINER = "vaws-workspace"
DEFAULT_RUNTIME_SSH_PORT = 63269
DEFAULT_RUNTIME_WORKSPACE_ROOT = "/vllm-workspace"
DEFAULT_RUNTIME_BOOTSTRAP_MODE = "host-then-container"
DEFAULT_SERVER_PORT = 22
DEFAULT_SERVER_AUTH_REF = "default-server-auth"
DEFAULT_GIT_AUTH_REF = "default-git-auth"
DEFAULT_SERVER_STATUS = "pending"
BOOTSTRAP_MODE_REMOTE_FIRST = "remote-first"
BOOTSTRAP_MODE_LOCAL_ONLY = "local-only"


class BootstrapError(RuntimeError):
    """Structured bootstrap input or remote-topology failure."""


@dataclass(frozen=True)
class BootstrapRequest:
    server_host: Optional[str]
    server_user: str
    vllm_ascend_origin_url: str
    server_port: int = DEFAULT_SERVER_PORT
    server_auth_mode: str = "ssh-key"
    server_auth_group: str = "default"
    server_password_env: Optional[str] = None
    server_key_path: Optional[str] = None
    target_name: str = DEFAULT_TARGET_NAME
    host_name: str = DEFAULT_HOST_NAME
    runtime_image: str = DEFAULT_RUNTIME_IMAGE
    runtime_container: str = DEFAULT_RUNTIME_CONTAINER
    runtime_ssh_port: int = DEFAULT_RUNTIME_SSH_PORT
    runtime_workspace_root: str = DEFAULT_RUNTIME_WORKSPACE_ROOT
    vllm_origin_url: Optional[str] = None
    git_auth_mode: str = "ssh-key"
    git_key_path: Optional[str] = None
    git_token_env: Optional[str] = None


def _require_non_empty(value: Optional[str], field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BootstrapError(f"missing bootstrap field: {field_name}")
    return value.strip()


def _optional_non_empty(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BootstrapError(f"invalid bootstrap field: {field_name}")
    return value.strip()


def _require_port(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 65535:
        raise BootstrapError(f"invalid bootstrap field: {field_name}")
    return value


def bootstrap_request_from_args(args: Any) -> BootstrapRequest:
    return BootstrapRequest(
        server_host=_optional_non_empty(args.server_host, "server host"),
        server_user=_require_non_empty(args.server_user, "server user"),
        vllm_ascend_origin_url=_require_non_empty(
            args.vllm_ascend_origin_url,
            "vllm-ascend origin url",
        ),
        server_port=_require_port(args.server_port, "server port"),
        server_auth_mode=_require_non_empty(args.server_auth_mode, "server auth mode"),
        server_auth_group=_require_non_empty(
            args.server_auth_group,
            "server auth group",
        ),
        server_password_env=args.server_password_env,
        server_key_path=args.server_key_path,
        target_name=_require_non_empty(args.target_name, "target name"),
        host_name=_require_non_empty(args.host_name, "host name"),
        runtime_image=_require_non_empty(args.runtime_image, "runtime image"),
        runtime_container=_require_non_empty(args.runtime_container, "runtime container"),
        runtime_ssh_port=_require_port(args.runtime_ssh_port, "runtime ssh port"),
        runtime_workspace_root=_require_non_empty(
            args.runtime_workspace_root,
            "runtime workspace root",
        ),
        vllm_origin_url=args.vllm_origin_url,
        git_auth_mode=_require_non_empty(args.git_auth_mode, "git auth mode"),
        git_key_path=args.git_key_path,
        git_token_env=args.git_token_env,
    )


def _bootstrap_mode(request: BootstrapRequest) -> str:
    return (
        BOOTSTRAP_MODE_LOCAL_ONLY
        if request.server_host is None
        else BOOTSTRAP_MODE_REMOTE_FIRST
    )


def _bootstrap_host_workspace_path(request: BootstrapRequest) -> str:
    return f"{DEFAULT_HOST_WORKSPACE_BASE}/{request.target_name}/workspace"


def _staged_init_request(request: BootstrapRequest) -> Any:
    from .init_flow import InitRequest

    return InitRequest(
        server_host=request.server_host,
        server_name=request.host_name if request.server_host is not None else None,
        local_only=request.server_host is None,
        server_user=request.server_user,
        server_port=request.server_port,
        server_auth_mode=request.server_auth_mode,
        server_password_env=request.server_password_env,
        server_key_path=request.server_key_path,
        runtime_image=request.runtime_image,
        runtime_container=request.runtime_container,
        runtime_ssh_port=request.runtime_ssh_port,
        runtime_workspace_root=request.runtime_workspace_root,
        runtime_bootstrap_mode=DEFAULT_RUNTIME_BOOTSTRAP_MODE,
        vllm_origin_url=request.vllm_origin_url,
        vllm_ascend_origin_url=request.vllm_ascend_origin_url,
        git_auth_mode=request.git_auth_mode,
        git_key_path=request.git_key_path,
        git_token_env=request.git_token_env,
    )


def _run_staged_init(paths: RepoPaths, request: Any) -> int:
    from .init_flow import run_init

    return run_init(paths, request)


def _preserve_requested_git_auth(paths: RepoPaths, request: BootstrapRequest) -> None:
    if (
        request.git_key_path is None
        and request.git_token_env is None
        and request.git_auth_mode not in {"token"}
    ):
        return

    write_git_auth_ref(
        paths,
        git_auth_mode=request.git_auth_mode,
        git_key_path=request.git_key_path,
        git_token_env=request.git_token_env,
    )


def verify_runtime(_paths: RepoPaths, context: TargetContext) -> VerificationResult:
    runtime = {
        "image_ref": context.runtime.image_ref,
        "container_name": context.runtime.container_name,
        "ssh_port": context.runtime.ssh_port,
        "workspace_root": context.runtime.workspace_root,
        "bootstrap_mode": context.runtime.bootstrap_mode,
        "transport": "bootstrap",
        "container_endpoint": (
            f"bootstrap://{context.host.name}/{context.runtime.container_name}"
        ),
        "host_name": context.host.name,
        "host": context.host.host,
        "host_port": context.host.port,
        "login_user": context.host.login_user,
        "host_workspace_path": context.runtime.host_workspace_path,
    }
    return VerificationResult.ready(
        summary=f"bootstrap inventory ready for {context.host.name}",
        runtime=runtime,
        checks=[
            VerificationCheck(
                name="inventory",
                status="ready",
                detail="bootstrap server inventory resolved",
            )
        ],
    )


def _ensure_overlay(paths: RepoPaths) -> None:
    if paths.local_overlay.exists() and not paths.local_overlay.is_dir():
        raise BootstrapError(
            "invalid local overlay: .workspace.local/ exists but is not a directory"
        )

    ensure_overlay_layout(paths)


def _load_yaml_mapping(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BootstrapError(f"invalid bootstrap file: {path.name}") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise BootstrapError(f"invalid bootstrap file: {path.name}")
    return loaded


def _write_yaml(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _auth_ref_payload(kind: str, **fields: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"kind": kind}
    for key, value in fields.items():
        if value is None:
            continue
        payload[key] = value
    return payload


def _load_auth_mapping(paths: RepoPaths) -> Dict[str, Any]:
    return _load_yaml_mapping(paths.local_auth_file)


def _auth_namespace_refs(auth: Dict[str, Any], namespace: str) -> Dict[str, Any]:
    section = auth.get(namespace)
    if section is None:
        section = {"refs": {}}
        auth[namespace] = section
    if not isinstance(section, dict):
        raise BootstrapError(f"invalid auth file: .workspace.local/auth.yaml {namespace}")

    refs = section.get("refs")
    if refs is None:
        refs = {}
        section["refs"] = refs
    if not isinstance(refs, dict):
        raise BootstrapError(f"invalid auth file: .workspace.local/auth.yaml {namespace}.refs")
    return refs


def _repo_topology_payload(
    *,
    vllm_origin_url: Optional[str],
    vllm_ascend_origin_url: str,
) -> Dict[str, Any]:
    topology: Dict[str, Any] = {
        "version": 1,
        "workspace": {
            "path": ".",
            "default_branch": "main",
            "protected_branches": ["main"],
            "push_remote": "origin",
            "upstream_remote": "upstream",
        },
        "submodules": {
            "vllm": {
                "path": "vllm",
                "default_branch": "main",
                "push_remote": "origin",
                "upstream_remote": "upstream",
                "upstream_url": COMMUNITY_UPSTREAM_URLS["vllm"],
            },
            "vllm-ascend": {
                "path": "vllm-ascend",
                "default_branch": "main",
                "push_remote": "origin",
                "upstream_remote": "upstream",
                "upstream_url": COMMUNITY_UPSTREAM_URLS["vllm-ascend"],
                "origin_url": vllm_ascend_origin_url,
            },
        },
    }
    if vllm_origin_url:
        topology["submodules"]["vllm"]["origin_url"] = vllm_origin_url
    return topology


def load_existing_repo_topology(paths: RepoPaths) -> Dict[str, Any]:
    return _load_yaml_mapping(paths.local_repos_file)


def topology_is_ready(paths: RepoPaths) -> bool:
    topology = load_existing_repo_topology(paths)
    if not topology:
        return False

    workspace = topology.get("workspace")
    submodules = topology.get("submodules")
    if not isinstance(workspace, dict) or not isinstance(submodules, dict):
        return False

    for repo_name in ("vllm", "vllm-ascend"):
        repo_config = submodules.get(repo_name)
        if not isinstance(repo_config, dict):
            return False

        repo_path_value = repo_config.get("path")
        if not isinstance(repo_path_value, str) or not repo_path_value.strip():
            return False

        repo_path = paths.root / repo_path_value.strip()
        if not repo_path.is_dir():
            return False

        expected_upstream = repo_config.get("upstream_url")
        if not isinstance(expected_upstream, str) or not expected_upstream.strip():
            return False

        if _git_remote_url(repo_path, "upstream") != expected_upstream.strip():
            return False
        expected_origin = repo_config.get("origin_url")
        if repo_name == "vllm" and expected_origin is None:
            continue
        if (
            repo_name == "vllm-ascend"
            and expected_origin == COMMUNITY_UPSTREAM_URLS["vllm-ascend"]
        ):
            return False
        if not isinstance(expected_origin, str) or not expected_origin.strip():
            return False
        if _git_remote_url(repo_path, "origin") != expected_origin.strip():
            return False

    return True


def write_repo_topology(
    paths: RepoPaths,
    *,
    vllm_origin_url: Optional[str],
    vllm_ascend_origin_url: str,
) -> None:
    _write_yaml(
        paths.local_repos_file,
        _repo_topology_payload(
            vllm_origin_url=vllm_origin_url,
            vllm_ascend_origin_url=vllm_ascend_origin_url,
        ),
    )


def write_server_auth_ref(
    paths: RepoPaths,
    *,
    server_auth_mode: str,
    server_user: str,
    server_password_env: Optional[str],
    server_key_path: Optional[str],
) -> None:
    auth = _load_auth_mapping(paths)
    auth.setdefault("version", 1)
    ssh_refs = _auth_namespace_refs(auth, "ssh_auth")
    ssh_refs[DEFAULT_SERVER_AUTH_REF] = _auth_ref_payload(
        server_auth_mode,
        username=server_user,
        password_env=server_password_env,
        key_path=server_key_path,
    )
    _write_yaml(paths.local_auth_file, auth)


def write_git_auth_ref(
    paths: RepoPaths,
    *,
    git_auth_mode: str,
    git_key_path: Optional[str],
    git_token_env: Optional[str],
) -> None:
    auth = _load_auth_mapping(paths)
    auth.setdefault("version", 1)
    git_refs = _auth_namespace_refs(auth, "git_auth")
    git_refs[DEFAULT_GIT_AUTH_REF] = _auth_ref_payload(
        git_auth_mode,
        key_path=git_key_path,
        token_env=git_token_env,
    )
    _write_yaml(paths.local_auth_file, auth)


def _run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
    )


def _git_remote_url(repo_path: Path, remote_name: str) -> Optional[str]:
    probe = _run_git(repo_path, "remote", "get-url", remote_name)
    if probe.returncode != 0:
        return None
    return probe.stdout.strip()


def _ensure_git_remote(repo_path: Path, remote_name: str, url: str) -> None:
    if not (repo_path / ".git").exists():
        raise BootstrapError(f"invalid workspace repo: {repo_path}")

    probe = _run_git(repo_path, "remote", "get-url", remote_name)
    if probe.returncode == 0:
        update = _run_git(repo_path, "remote", "set-url", remote_name, url)
    else:
        update = _run_git(repo_path, "remote", "add", remote_name, url)

    if update.returncode != 0:
        stderr = update.stderr.strip() or update.stdout.strip()
        raise BootstrapError(
            f"failed to configure remote '{remote_name}' for {repo_path.name}: {stderr}"
        )


def _write_repos_yaml(paths: RepoPaths, request: BootstrapRequest) -> None:
    write_repo_topology(
        paths,
        vllm_origin_url=request.vllm_origin_url,
        vllm_ascend_origin_url=request.vllm_ascend_origin_url,
    )


def _bootstrap_servers_config(
    request: BootstrapRequest,
    *,
    completed: bool,
) -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "version": 1,
        "bootstrap": {
            "completed": completed,
            "mode": _bootstrap_mode(request),
        },
        "servers": {},
    }
    if request.server_host is not None:
        config["servers"][request.host_name] = {
            "host": request.server_host,
            "port": request.server_port,
            "login_user": request.server_user,
            "ssh_auth_ref": DEFAULT_SERVER_AUTH_REF,
            "status": DEFAULT_SERVER_STATUS,
            "runtime": {
                "image_ref": request.runtime_image,
                "container_name": request.runtime_container,
                "ssh_port": request.runtime_ssh_port,
                "workspace_root": request.runtime_workspace_root,
                "bootstrap_mode": DEFAULT_RUNTIME_BOOTSTRAP_MODE,
                "host_workspace_path": _bootstrap_host_workspace_path(request),
            },
        }
    return config


def _write_servers_yaml(paths: RepoPaths, request: BootstrapRequest, *, completed: bool) -> None:
    _write_yaml(paths.local_servers_file, _bootstrap_servers_config(request, completed=completed))


def _write_targets_yaml(paths: RepoPaths, request: BootstrapRequest) -> None:
    targets_path = paths.local_targets_file
    if request.server_host is None:
        _write_yaml(
            targets_path,
            {
                "version": 1,
                "hosts": {},
                "targets": {},
            },
        )
        return

    _write_yaml(
        targets_path,
        {
            "version": 1,
            "hosts": {
                request.host_name: {
                    "host": request.server_host,
                    "port": request.server_port,
                    "login_user": request.server_user,
                    "ssh_auth_ref": DEFAULT_SERVER_AUTH_REF,
                },
            },
            "targets": {
                request.target_name: {
                    "hosts": [request.host_name],
                    "runtime": {
                        "image_ref": request.runtime_image,
                        "container_name": request.runtime_container,
                        "ssh_port": request.runtime_ssh_port,
                        "workspace_root": request.runtime_workspace_root,
                        "bootstrap_mode": DEFAULT_RUNTIME_BOOTSTRAP_MODE,
                        "host_workspace_path": _bootstrap_host_workspace_path(request),
                    },
                },
            },
        },
    )


def _write_auth_yaml(paths: RepoPaths, request: BootstrapRequest) -> None:
    auth_path = paths.local_auth_file
    if request.server_host is None:
        _write_yaml(
            auth_path,
            {
                "version": 1,
                "ssh_auth": {"refs": {}},
                "git_auth": {"refs": {}},
            },
        )
        return

    write_server_auth_ref(
        paths,
        server_auth_mode=request.server_auth_mode,
        server_user=request.server_user,
        server_password_env=request.server_password_env,
        server_key_path=request.server_key_path,
    )
    write_git_auth_ref(
        paths,
        git_auth_mode=request.git_auth_mode,
        git_key_path=request.git_key_path,
        git_token_env=request.git_token_env,
    )


def _read_bootstrap_state(paths: RepoPaths) -> Dict[str, Any]:
    try:
        return read_state(paths)
    except RuntimeError as exc:
        raise BootstrapError(str(exc)) from exc


def _ensure_bootstrap_not_completed(paths: RepoPaths) -> None:
    state = _read_bootstrap_state(paths)
    bootstrap_state = state.get("bootstrap")
    if not isinstance(bootstrap_state, dict):
        return

    if bootstrap_state.get("completed") is True:
        raise BootstrapError(
            "bootstrap baseline already completed; use `vaws fleet` for later server changes"
        )


def _write_bootstrap_state(paths: RepoPaths, request: BootstrapRequest) -> None:
    try:
        state = read_state(paths)
        state["bootstrap"] = {
            "completed": True,
            "mode": _bootstrap_mode(request),
        }
        write_state(paths, state)
    except RuntimeError as exc:
        raise BootstrapError(str(exc)) from exc


def _set_bootstrap_completed(paths: RepoPaths, completed: bool) -> None:
    servers_state = _load_yaml_mapping(paths.local_servers_file)
    bootstrap_state = servers_state.get("bootstrap")
    if not isinstance(bootstrap_state, dict):
        raise BootstrapError("invalid server config: missing bootstrap map")

    bootstrap_state["completed"] = completed
    servers_state["bootstrap"] = bootstrap_state
    _write_yaml(paths.local_servers_file, servers_state)


def _finalize_bootstrap(paths: RepoPaths, request: BootstrapRequest) -> None:
    try:
        _write_bootstrap_state(paths, request)
        _set_bootstrap_completed(paths, True)
        servers_state = _load_yaml_mapping(paths.local_servers_file)
        state = _read_bootstrap_state(paths)
        if (
            state.get("bootstrap", {}).get("completed") is not True
            or servers_state.get("bootstrap", {}).get("completed") is not True
        ):
            raise BootstrapError("bootstrap baseline finalization verification failed")
    except BootstrapError as exc:
        rollback_state = _read_bootstrap_state(paths)
        rollback_state["bootstrap"] = {
            "completed": False,
            "mode": _bootstrap_mode(request),
        }
        try:
            write_state(paths, rollback_state)
        except RuntimeError:
            pass
        try:
            _set_bootstrap_completed(paths, False)
        except BootstrapError:
            pass
        raise
    except RuntimeError as exc:
        raise BootstrapError(str(exc)) from exc


def configure_repo_remotes(
    paths: RepoPaths,
    *,
    vllm_origin_url: Optional[str],
    vllm_ascend_origin_url: str,
) -> None:
    repo_targets = {
        "vllm": {
            "path": paths.root / "vllm",
            "upstream": COMMUNITY_UPSTREAM_URLS["vllm"],
            "origin": vllm_origin_url,
        },
        "vllm-ascend": {
            "path": paths.root / "vllm-ascend",
            "upstream": COMMUNITY_UPSTREAM_URLS["vllm-ascend"],
            "origin": vllm_ascend_origin_url,
        },
    }

    for repo_name, config in repo_targets.items():
        repo_path = config["path"]
        _ensure_git_remote(repo_path, "upstream", config["upstream"])
        origin_url = config["origin"]
        if origin_url:
            _ensure_git_remote(repo_path, "origin", origin_url)


def bootstrap_init(paths: RepoPaths, request: BootstrapRequest) -> int:
    try:
        staged_request = _staged_init_request(request)
        result = _run_staged_init(paths, staged_request)
        if result != 0:
            return result
        _preserve_requested_git_auth(paths, request)
        return 0
    except BootstrapError as exc:
        print(str(exc))
        return 1
