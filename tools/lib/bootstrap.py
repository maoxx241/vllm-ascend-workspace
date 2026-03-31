from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .config import RepoPaths
from .overlay import ensure_overlay_layout
from .preflight import PreflightError, ensure_local_control_plane_deps
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
    _write_yaml(
        paths.local_repos_file,
        {
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
                    "origin_url": request.vllm_ascend_origin_url,
                },
            },
        },
    )
    if request.vllm_origin_url:
        repos_path = paths.local_repos_file
        config = _load_yaml_mapping(repos_path)
        config["submodules"]["vllm"]["origin_url"] = request.vllm_origin_url
        _write_yaml(repos_path, config)


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

    _write_yaml(
        auth_path,
        {
            "version": 1,
            "ssh_auth": {
                "refs": {
                    DEFAULT_SERVER_AUTH_REF: _auth_ref_payload(
                        request.server_auth_mode,
                        username=request.server_user,
                        password_env=request.server_password_env,
                        key_path=request.server_key_path,
                    ),
                },
            },
            "git_auth": {
                "refs": {
                    DEFAULT_GIT_AUTH_REF: _auth_ref_payload(
                        request.git_auth_mode,
                        key_path=request.git_key_path,
                        token_env=request.git_token_env,
                    ),
                },
            },
        },
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


def _finalize_bootstrap(paths: RepoPaths, request: BootstrapRequest) -> None:
    try:
        _write_servers_yaml(paths, request, completed=True)
        _write_bootstrap_state(paths, request)
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
            _write_servers_yaml(paths, request, completed=False)
        except BootstrapError:
            pass
        raise
    except RuntimeError as exc:
        raise BootstrapError(str(exc)) from exc


def _configure_repo_remotes(paths: RepoPaths, request: BootstrapRequest) -> None:
    repo_targets = {
        "vllm": {
            "path": paths.root / "vllm",
            "upstream": COMMUNITY_UPSTREAM_URLS["vllm"],
            "origin": request.vllm_origin_url,
        },
        "vllm-ascend": {
            "path": paths.root / "vllm-ascend",
            "upstream": COMMUNITY_UPSTREAM_URLS["vllm-ascend"],
            "origin": request.vllm_ascend_origin_url,
        },
    }

    for repo_name, config in repo_targets.items():
        repo_path = config["path"]
        _ensure_git_remote(repo_path, "upstream", config["upstream"])
        origin_url = config["origin"]
        if origin_url:
            _ensure_git_remote(repo_path, "origin", origin_url)
        elif repo_name == "vllm":
            _ensure_git_remote(repo_path, "origin", config["upstream"])


def bootstrap_init(paths: RepoPaths, request: BootstrapRequest) -> int:
    try:
        preflight_report = ensure_local_control_plane_deps()
        _ensure_overlay(paths)
        _ensure_bootstrap_not_completed(paths)
        _write_repos_yaml(paths, request)
        _write_servers_yaml(paths, request, completed=False)
        _write_targets_yaml(paths, request)
        _write_auth_yaml(paths, request)
        _configure_repo_remotes(paths, request)
        _finalize_bootstrap(paths, request)
    except PreflightError as exc:
        print(str(exc))
        return 1
    except BootstrapError as exc:
        print(str(exc))
        return 1

    if preflight_report.status == "degraded":
        missing = ", ".join(preflight_report.missing_recommended)
        print(f"init: preflight degraded: missing recommended tools: {missing}")
    print(f"init: bootstrap ok ({_bootstrap_mode(request)})")
    return 0
