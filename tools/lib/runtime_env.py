from __future__ import annotations

import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict

from .code_parity import verify_code_parity
from .config import RepoPaths
from .remote_types import RemoteError
from .repo_targets import WorkspaceTargets
from .runtime_transport import resolve_available_runtime_transport, run_container_command
from .target_context import resolve_server_context

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


def ensure_runtime_environment(
    paths: RepoPaths,
    server_name: str,
    desired: WorkspaceTargets,
) -> RuntimeEnvironmentResult:
    parity = verify_code_parity(paths, server_name, desired)
    needs_rebuild = parity.status != "ready"

    ctx = resolve_server_context(paths, server_name)
    try:
        transport = resolve_available_runtime_transport(ctx)
    except RemoteError as exc:
        return RuntimeEnvironmentResult(
            status="needs_repair",
            summary=str(exc),
            installed=False,
            rebuilt=False,
        )

    if not needs_rebuild:
        return RuntimeEnvironmentResult(
            status="ready",
            summary=f"runtime environment already ready for {server_name}",
            installed=False,
            rebuilt=False,
        )

    result = run_container_command(ctx, transport, _install_script(ctx.runtime.workspace_root))
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())

    return RuntimeEnvironmentResult(
        status="ready",
        summary=f"runtime environment rebuilt for {server_name}",
        installed=True,
        rebuilt=True,
    )
