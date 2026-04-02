from __future__ import annotations

import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict

from .config import RepoPaths
from .remote import resolve_server_context, run_runtime_command
from .repo_targets import WorkspaceTargets
from .runtime import read_state, require_container_ssh_transport, update_state

PYTHON_BIN = "/usr/local/python3.11.14/bin/python3"
PIP_BIN = "/usr/local/python3.11.14/bin/pip"

NATIVE_PATH_MARKERS = (
    "csrc/",
    "cmake/",
    "vllm_ascend/_cann_ops_custom/",
)
NATIVE_FILE_SUFFIXES = (
    ".c",
    ".cc",
    ".cpp",
    ".cuh",
    ".cu",
    ".cxx",
    ".h",
    ".hpp",
)
BUILD_SENSITIVE_FILES = {
    "CMakeLists.txt",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
}


@dataclass(frozen=True)
class RuntimeEnvironmentResult:
    status: str
    summary: str
    installed: bool
    rebuilt: bool

    def to_mapping(self) -> Dict[str, Any]:
        return asdict(self)


def _git_changed_paths(repo_path: Path, previous_commit: str, current_commit: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", previous_commit, current_commit],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git diff failed").strip())
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _path_requires_reinstall(path: str) -> bool:
    normalized = path.strip()
    if not normalized:
        return False
    if normalized in BUILD_SENSITIVE_FILES:
        return True
    if normalized.endswith(NATIVE_FILE_SUFFIXES):
        return True
    return any(normalized.startswith(marker) for marker in NATIVE_PATH_MARKERS)


def _repo_reinstall_required(
    repo_path: Path,
    previous_commit: str | None,
    current_commit: str,
) -> bool:
    if not previous_commit or previous_commit == current_commit:
        return previous_commit is None
    changed_paths = _git_changed_paths(repo_path, previous_commit, current_commit)
    return any(_path_requires_reinstall(path) for path in changed_paths)


def _install_script(workspace_root: str) -> str:
    workspace_path = shlex.quote(f"{workspace_root}/workspace")
    env_script_path = shlex.quote(
        f"{workspace_root}/workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash"
    )
    vllm_path = shlex.quote(f"{workspace_root}/workspace/vllm")
    vllm_ascend_path = shlex.quote(f"{workspace_root}/workspace/vllm-ascend")
    return "\n".join(
        [
            "set -euo pipefail",
            f"cd {workspace_path}",
            "source /usr/local/Ascend/ascend-toolkit/set_env.sh",
            "source /usr/local/Ascend/nnal/atb/set_env.sh",
            f"source {env_script_path}",
            f"export PATH={Path(PYTHON_BIN).parent}:$PATH",
            f"export PYTHON={PYTHON_BIN}",
            f"export PIP={PIP_BIN}",
            f"{PIP_BIN} uninstall -y vllm vllm-ascend || true",
            f"cd {vllm_path}",
            "export VLLM_TARGET_DEVICE=empty",
            f"{PIP_BIN} install -e . --no-build-isolation",
            f"cd {vllm_ascend_path}",
            f"{PIP_BIN} install -r requirements.txt",
            f"{PIP_BIN} install -v -e . --no-build-isolation",
        ]
    )


def _persist_runtime_environment(
    paths: RepoPaths,
    server_name: str,
    desired: WorkspaceTargets,
    result: RuntimeEnvironmentResult,
) -> None:
    state = read_state(paths)
    existing = state.get("runtime_environment")
    if not isinstance(existing, dict):
        existing = {}
    existing[server_name] = {
        "status": result.status,
        "summary": result.summary,
        "workspace_commit": desired.workspace.commit,
        "vllm_commit": desired.vllm.commit,
        "vllm_ascend_commit": desired.vllm_ascend.commit,
        "installed": result.installed,
        "rebuilt": result.rebuilt,
    }
    update_state(paths, runtime_environment=existing)


def ensure_runtime_environment(
    paths: RepoPaths,
    server_name: str,
    desired: WorkspaceTargets,
) -> RuntimeEnvironmentResult:
    state = read_state(paths)
    runtime_environment = state.get("runtime_environment")
    previous = runtime_environment.get(server_name) if isinstance(runtime_environment, dict) else None
    previous_vllm = previous.get("vllm_commit") if isinstance(previous, dict) else None
    previous_vllm_ascend = previous.get("vllm_ascend_commit") if isinstance(previous, dict) else None
    first_install = not isinstance(previous, dict)
    needs_rebuild = first_install or _repo_reinstall_required(
        paths.root / "vllm",
        previous_vllm,
        desired.vllm.commit,
    ) or _repo_reinstall_required(
        paths.root / "vllm-ascend",
        previous_vllm_ascend,
        desired.vllm_ascend.commit,
    )

    try:
        transport = require_container_ssh_transport(paths, server_name)
    except RuntimeError as exc:
        env_result = RuntimeEnvironmentResult(
            status="needs_repair",
            summary=str(exc),
            installed=False,
            rebuilt=False,
        )
        _persist_runtime_environment(paths, server_name, desired, env_result)
        return env_result

    if not needs_rebuild:
        result = RuntimeEnvironmentResult(
            status="ready",
            summary=f"runtime environment already ready for {server_name}",
            installed=False,
            rebuilt=False,
        )
        _persist_runtime_environment(paths, server_name, desired, result)
        return result

    ctx = resolve_server_context(paths, server_name)
    result = run_runtime_command(ctx, transport, _install_script(ctx.runtime.workspace_root))
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())

    env_result = RuntimeEnvironmentResult(
        status="ready",
        summary=(
            f"runtime environment bootstrapped for {server_name}"
            if first_install
            else f"runtime environment rebuilt for {server_name}"
        ),
        installed=first_install,
        rebuilt=not first_install,
    )
    _persist_runtime_environment(paths, server_name, desired, env_result)
    return env_result
