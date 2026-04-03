from __future__ import annotations

import json
import shlex
from typing import Any

from .remote_types import TargetContext
from .runtime_transport import run_container_command


def remote_service_manifest_dir(workspace_root: str) -> str:
    return f"{workspace_root}/artifacts/services"


def remote_service_manifest_path(workspace_root: str, service_id: str) -> str:
    return f"{remote_service_manifest_dir(workspace_root)}/{service_id}.json"


def parse_service_manifest_lines(stdout: str) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _write_manifest_script(manifest_path: str, payload: dict[str, object]) -> str:
    manifest_dir = manifest_path.rsplit("/", 1)[0]
    serialized = json.dumps(payload, sort_keys=True)
    return "\n".join(
        [
            "set -euo pipefail",
            f"mkdir -p {shlex.quote(manifest_dir)}",
            f"cat > {shlex.quote(manifest_path)} <<'EOF'",
            serialized,
            "EOF",
        ]
    )


def _list_manifests_script(workspace_root: str) -> str:
    manifest_dir = remote_service_manifest_dir(workspace_root)
    return "\n".join(
        [
            "set -euo pipefail",
            f"if [ ! -d {shlex.quote(manifest_dir)} ]; then exit 0; fi",
            "python3 - <<'PY'",
            "import glob",
            "import json",
            f"manifest_dir = {json.dumps(manifest_dir)}",
            "for path in sorted(glob.glob(manifest_dir + '/*.json')):",
            "    with open(path, 'r', encoding='utf-8') as handle:",
            "        payload = json.load(handle)",
            "    if not isinstance(payload, dict):",
            "        continue",
            "    payload.setdefault('manifest_path', path)",
            "    print(json.dumps(payload, sort_keys=True))",
            "PY",
        ]
    )


def write_service_manifest(
    ctx: TargetContext,
    transport: str,
    payload: dict[str, object],
) -> str:
    service_id = str(payload["service_id"])
    manifest_path = str(payload.get("manifest_path") or remote_service_manifest_path(ctx.runtime.workspace_root, service_id))
    serialized = dict(payload)
    serialized["manifest_path"] = manifest_path
    result = run_container_command(ctx, transport, _write_manifest_script(manifest_path, serialized))
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"failed to write service manifest for {service_id}")
    return manifest_path


def list_service_manifests(ctx: TargetContext, transport: str) -> list[dict[str, object]]:
    result = run_container_command(ctx, transport, _list_manifests_script(ctx.runtime.workspace_root))
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"failed to list service manifests for {ctx.name}")
    return parse_service_manifest_lines(result.stdout)


def remove_service_manifest(ctx: TargetContext, transport: str, service_id: str) -> None:
    manifest_path = remote_service_manifest_path(ctx.runtime.workspace_root, service_id)
    script = "\n".join(
        [
            "set -euo pipefail",
            f"rm -f {shlex.quote(manifest_path)}",
        ]
    )
    result = run_container_command(ctx, transport, script)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"failed to remove service manifest for {service_id}")
