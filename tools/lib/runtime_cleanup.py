from __future__ import annotations

from .config import RepoPaths
from .host_access import run_host_command
from .remote_types import CleanupResult, RemoteError, TargetContext
from .target_context import resolve_server_context


def destroy_host_runtime(ctx: TargetContext) -> None:
    script = "\n".join(
        [
            "set -e",
            f"container_names=$(docker container ls -a --format '{{{{.Names}}}}') || exit 1",
            f"if printf '%s\\n' \"$container_names\" | grep -Fqx {ctx.runtime.container_name!r}; then",
            f"  docker rm -f {ctx.runtime.container_name!r} >/dev/null",
            "fi",
            f"rm -rf {ctx.runtime.host_workspace_path!r}",
        ]
    )
    result = run_host_command(ctx, script)
    if result.returncode != 0:
        raise RemoteError(
            f"failed to clean remote runtime for target '{ctx.name}': {(result.stderr or result.stdout).strip()}"
        )


def _host_runtime_status(ctx: TargetContext) -> str:
    script = "\n".join(
        [
            "set -e",
            "container_names=$(docker container ls -a --format '{{.Names}}')",
            f"if printf '%s\\n' \"$container_names\" | grep -Fqx {ctx.runtime.container_name!r}; then",
            "  echo present",
            f"elif [ -e {ctx.runtime.host_workspace_path!r} ]; then",
            "  echo present",
            "else",
            "  echo absent",
            "fi",
        ]
    )
    result = run_host_command(ctx, script)
    if result.returncode != 0:
        raise RemoteError(
            f"failed to inspect remote runtime for server '{ctx.name}': {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip() or "present"


def _is_connectivity_failure(message: str) -> bool:
    lowered = message.lower()
    return any(token in lowered for token in ("probe", "ssh", "auth", "authenticate"))


def cleanup_runtime(ctx: TargetContext) -> CleanupResult:
    try:
        status = _host_runtime_status(ctx)
    except RemoteError as exc:
        message = str(exc)
        if _is_connectivity_failure(message):
            return CleanupResult(
                server_name=ctx.name,
                status="unreachable",
                detail=message,
            )
        return CleanupResult(
            server_name=ctx.name,
            status="cleanup_failed",
            detail=message,
        )

    if status == "absent":
        return CleanupResult(
            server_name=ctx.name,
            status="already_absent",
            detail="remote runtime already absent",
        )

    try:
        destroy_host_runtime(ctx)
    except RemoteError as exc:
        message = str(exc)
        if _is_connectivity_failure(message):
            return CleanupResult(
                server_name=ctx.name,
                status="unreachable",
                detail=message,
            )
        return CleanupResult(
            server_name=ctx.name,
            status="cleanup_failed",
            detail=message,
        )

    return CleanupResult(
        server_name=ctx.name,
        status="removed",
        detail="remote runtime removed",
    )


def cleanup_runtime_for_server(paths: RepoPaths, server_name: str) -> CleanupResult:
    return cleanup_runtime(resolve_server_context(paths, server_name))
