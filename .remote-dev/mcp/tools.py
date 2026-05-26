from __future__ import annotations

from pathlib import Path
import json
import re
import shlex
import sys
from typing import Any

SUBSTRATE_ROOT = Path(__file__).resolve().parents[1]
if str(SUBSTRATE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUBSTRATE_ROOT))

from core.artifact_ops import remote_artifact_manifest, remote_artifact_pull, remote_artifact_push  # noqa: E402
from core.context_snapshot import remote_context_snapshot, remote_probe  # noqa: E402
from core.endpoint import resolve_endpoint  # noqa: E402
from core.file_ops import remote_edit, remote_ls, remote_multi_edit, remote_read, remote_write  # noqa: E402
from core.job_ops import endpoint_from_job_record, require_job_id, remote_job_status, remote_job_stop, remote_job_tail  # noqa: E402
from core.monitor_ops import remote_monitor  # noqa: E402
from core.patch_ops import remote_apply_patch  # noqa: E402
from core.search_ops import remote_glob, remote_grep  # noqa: E402
from core.shell_ops import remote_bash  # noqa: E402
from core.ssh_transport import run_script  # noqa: E402
from core.state_store import (  # noqa: E402
    artifacts_dir,
    jobs_dir,
    latest_context_path,
    list_endpoint_records,
    list_job_records,
    read_text_if_exists,
    state_root,
)
from mcp.schemas import ALIASES, TOOL_SCHEMAS  # noqa: E402

ENDPOINT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
RESOURCE_LOG_LIMIT_BYTES = 150000


def list_tools() -> list[dict[str, Any]]:
    descriptions = {
        "remote.read": "Read a remote file with native Read-like pagination and a read ledger.",
        "remote.write": "Write a remote file with overwrite support and optional read-ledger concurrency checks.",
        "remote.edit": "Edit a remote file with exact string replacement and optional read-ledger concurrency checks.",
        "remote.multi_edit": "Apply multiple exact edits atomically to one remote file.",
        "remote.bash": "Run a remote shell command with Bash-like semantics, logs, preview, and optional background job.",
        "remote.glob": "Find remote paths with ** glob semantics.",
        "remote.grep": "Search remote files with rg-compatible semantics and Python fallback.",
        "remote.ls": "List a remote directory without reading file contents.",
        "remote.monitor": "Start a background remote command for monitoring.",
        "remote.apply_patch": "Apply a Codex apply_patch payload or unified diff on a remote endpoint.",
        "remote.job_status": "Check remote background job status.",
        "remote.job_tail": "Tail remote background job logs.",
        "remote.job_stop": "Stop a remote background job.",
        "remote.artifact_manifest": "Build a remote artifact sha256 manifest.",
        "remote.artifact_pull": "Pull a remote artifact through SSH streaming with hash verification.",
        "remote.artifact_push": "Push a local artifact through SSH streaming with hash verification.",
        "remote.context_snapshot": "Write a compact endpoint context snapshot.",
        "remote.probe": "Probe basic endpoint facts.",
    }
    return [
        {"name": name, "description": descriptions.get(name, name), "inputSchema": TOOL_SCHEMAS[name]}
        for name in TOOL_SCHEMAS
    ]


def list_resources() -> list[dict[str, Any]]:
    resources = [{"uri": "remote://endpoints", "name": "Remote endpoints", "mimeType": "application/json"}]
    for endpoint in list_endpoint_records():
        endpoint_id = str(endpoint.get("endpoint_id"))
        resources.append({
            "uri": f"remote://endpoint/{endpoint_id}/context/latest",
            "name": f"Remote context {endpoint_id}",
            "mimeType": "application/json",
        })
        resources.append({
            "uri": f"remote://endpoint/{endpoint_id}/jobs",
            "name": f"Remote jobs {endpoint_id}",
            "mimeType": "application/json",
        })
        for job in list_job_records(endpoint_id):
            job_id = str(job.get("job_id") or "")
            if not job_id:
                continue
            resources.extend([
                {
                    "uri": f"remote://endpoint/{endpoint_id}/job/{job_id}/status",
                    "name": f"Remote job status {job_id}",
                    "mimeType": "application/json",
                },
                {
                    "uri": f"remote://endpoint/{endpoint_id}/job/{job_id}/stdout",
                    "name": f"Remote job stdout {job_id}",
                    "mimeType": "text/plain",
                },
                {
                    "uri": f"remote://endpoint/{endpoint_id}/job/{job_id}/stderr",
                    "name": f"Remote job stderr {job_id}",
                    "mimeType": "text/plain",
                },
            ])
        resources.append({
            "uri": f"remote://endpoint/{endpoint_id}/artifacts",
            "name": f"Remote artifacts {endpoint_id}",
            "mimeType": "application/json",
        })
        directory = artifacts_dir(endpoint_id)
        if directory.exists():
            for path in sorted(directory.rglob("manifest.json")):
                artifact_id = path.parent.name
                if ARTIFACT_ID_RE.fullmatch(artifact_id):
                    resources.append({
                        "uri": f"remote://endpoint/{endpoint_id}/artifacts/{artifact_id}/manifest",
                        "name": f"Remote artifact manifest {artifact_id}",
                        "mimeType": "application/json",
                    })
    return resources


def _require_endpoint_id(value: str) -> str:
    if not ENDPOINT_ID_RE.fullmatch(value):
        raise KeyError(f"invalid endpoint id in resource: {value}")
    return value


def _job_record(endpoint_id: str, job_id: str) -> dict[str, Any]:
    require_job_id(job_id)
    records = {str(item.get("job_id")): item for item in list_job_records(endpoint_id)}
    record = records.get(job_id)
    if not record:
        raise KeyError(f"unknown job resource: {endpoint_id}/{job_id}")
    return record


def _read_job_log(record: dict[str, Any], stream: str) -> str:
    endpoint = endpoint_from_job_record(record)
    log_path = str(record.get("remote_dir", "")).rstrip("/") + f"/{stream}.log"
    script = (
        f"path={shlex.quote(log_path)}\n"
        "if [ ! -f \"$path\" ]; then exit 0; fi\n"
        "size=$(wc -c < \"$path\" 2>/dev/null || echo 0)\n"
        f"head -c {RESOURCE_LOG_LIMIT_BYTES} \"$path\"\n"
        f"if [ \"$size\" -gt {RESOURCE_LOG_LIMIT_BYTES} ]; then "
        f"printf '\\n<remote-dev resource truncated at {RESOURCE_LOG_LIMIT_BYTES} bytes>\\n'; fi\n"
    )
    completed = run_script(endpoint, script, timeout_ms=20000)
    if completed.returncode != 0 or completed.timed_out:
        return completed.stderr or completed.stdout or "failed to read remote job log"
    return completed.stdout


def _read_artifact_manifest(endpoint_id: str, artifact_id: str) -> str:
    if not ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise KeyError(f"invalid artifact id in resource: {artifact_id}")
    path = artifacts_dir(endpoint_id) / artifact_id / "manifest.json"
    text = read_text_if_exists(path)
    if text is None:
        raise KeyError(f"unknown artifact manifest resource: {endpoint_id}/{artifact_id}")
    return text


def read_resource(uri: str) -> dict[str, Any]:
    if uri == "remote://endpoints":
        return {"uri": uri, "mimeType": "application/json", "text": json.dumps({"endpoints": list_endpoint_records()}, ensure_ascii=False, indent=2, sort_keys=True)}
    prefix = "remote://endpoint/"
    if not uri.startswith(prefix):
        raise KeyError(f"unknown resource: {uri}")
    suffix = uri[len(prefix):]
    parts = suffix.split("/")
    endpoint_id = _require_endpoint_id(parts[0])
    if len(parts) >= 3 and parts[1:] == ["context", "latest"]:
        path = latest_context_path(endpoint_id)
        text = read_text_if_exists(path) or "{}"
        return {"uri": uri, "mimeType": "application/json", "text": text}
    if len(parts) == 2 and parts[1] == "jobs":
        return {"uri": uri, "mimeType": "application/json", "text": json.dumps({"jobs": list_job_records(endpoint_id)}, ensure_ascii=False, indent=2, sort_keys=True)}
    if len(parts) >= 3 and parts[1] == "job":
        job_id = parts[2]
        record = _job_record(endpoint_id, job_id)
        if len(parts) == 4 and parts[3] == "status":
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)}
        if len(parts) == 4 and parts[3] in {"stdout", "stderr"}:
            return {"uri": uri, "mimeType": "text/plain", "text": _read_job_log(record, parts[3])}
    if len(parts) == 2 and parts[1] == "artifacts":
        directory = artifacts_dir(endpoint_id)
        manifests = []
        if directory.exists():
            for path in sorted(directory.rglob("manifest.json")):
                manifests.append({
                    "artifact_id": path.parent.name,
                    "uri": f"remote://endpoint/{endpoint_id}/artifacts/{path.parent.name}/manifest",
                    "path": str(path),
                    "relative_path": str(path.relative_to(state_root())),
                })
        return {"uri": uri, "mimeType": "application/json", "text": json.dumps({"artifacts_dir": str(directory), "manifests": manifests}, ensure_ascii=False, indent=2, sort_keys=True)}
    if len(parts) == 4 and parts[1] == "artifacts":
        if len(parts) == 4 and parts[3] == "manifest":
            return {"uri": uri, "mimeType": "application/json", "text": _read_artifact_manifest(endpoint_id, parts[2])}
    if len(parts) >= 2 and parts[1] == "jobs-dir":
        return {"uri": uri, "mimeType": "text/plain", "text": str(jobs_dir(endpoint_id)) + "\n"}
    raise KeyError(f"unknown resource: {uri}")


def canonical_name(name: str) -> str:
    return ALIASES.get(name, name)


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    args = arguments or {}
    name = canonical_name(name)
    endpoint = None
    if name not in {"remote.job_status", "remote.job_tail", "remote.job_stop"} or any(args.get(k) for k in ("host", "port", "alias", "session_id", "session_file", "machine")):
        endpoint = resolve_endpoint(args)
    timeout_ms = int(args.get("timeout_ms") or args.get("timeout") or 120000)
    if name == "remote.bash":
        assert endpoint is not None
        return remote_bash(endpoint, command=str(args["command"]), cwd=args.get("cwd"), description=args.get("description"), timeout_ms=timeout_ms, run_in_background=bool(args.get("run_in_background", False)), runtime_env=args.get("runtime_env"), env=args.get("env") if isinstance(args.get("env"), dict) else {})
    if name == "remote.monitor":
        assert endpoint is not None
        return remote_monitor(endpoint, command=str(args["command"]), cwd=args.get("cwd"), description=args.get("description"), timeout_ms=timeout_ms, pattern=args.get("pattern"), runtime_env=args.get("runtime_env"), env=args.get("env") if isinstance(args.get("env"), dict) else {})
    if name == "remote.read":
        assert endpoint is not None
        return remote_read(endpoint, file_path=str(args["file_path"]), offset=int(args.get("offset") or 1), limit=int(args.get("limit") or 200), allow_symlink=bool(args.get("allow_symlink", False)), client_context_id=args.get("client_context_id"), timeout_ms=timeout_ms)
    if name == "remote.write":
        assert endpoint is not None
        return remote_write(endpoint, file_path=str(args["file_path"]), content=str(args.get("content", "")), overwrite=bool(args.get("overwrite", False)), create_dirs=bool(args.get("create_dirs", False)), client_context_id=args.get("client_context_id"), timeout_ms=timeout_ms)
    if name == "remote.edit":
        assert endpoint is not None
        return remote_edit(endpoint, file_path=str(args["file_path"]), old_string=str(args["old_string"]), new_string=str(args["new_string"]), replace_all=bool(args.get("replace_all", False)), client_context_id=args.get("client_context_id"), timeout_ms=timeout_ms)
    if name == "remote.multi_edit":
        assert endpoint is not None
        return remote_multi_edit(endpoint, file_path=str(args["file_path"]), edits=list(args.get("edits") or []), client_context_id=args.get("client_context_id"), timeout_ms=timeout_ms)
    if name == "remote.glob":
        assert endpoint is not None
        return remote_glob(endpoint, pattern=str(args["pattern"]), path=args.get("path"), limit=int(args.get("limit") or 100), respect_gitignore=bool(args.get("respect_gitignore", False)), timeout_ms=timeout_ms)
    if name == "remote.grep":
        assert endpoint is not None
        return remote_grep(endpoint, pattern=str(args["pattern"]), path=args.get("path"), glob=args.get("glob"), type=args.get("type"), output_mode=str(args.get("output_mode") or "files_with_matches"), multiline=bool(args.get("multiline", False)), limit=int(args.get("limit") or 100), timeout_ms=timeout_ms)
    if name == "remote.ls":
        assert endpoint is not None
        return remote_ls(endpoint, path=args.get("path"), limit=int(args.get("limit") or 200), all=bool(args.get("all", False)), timeout_ms=timeout_ms)
    if name == "remote.apply_patch":
        assert endpoint is not None
        return remote_apply_patch(endpoint, patch=args.get("patch"), command=args.get("command"), cwd=args.get("cwd"), timeout_ms=timeout_ms)
    if name == "remote.job_status":
        return remote_job_status(endpoint, job_id=str(args["job_id"]))
    if name == "remote.job_tail":
        return remote_job_tail(endpoint, job_id=str(args["job_id"]), lines=int(args.get("lines") or 80), stream=str(args.get("stream") or "both"))
    if name == "remote.job_stop":
        return remote_job_stop(endpoint, job_id=str(args["job_id"]), force=bool(args.get("force", False)))
    if name == "remote.artifact_manifest":
        assert endpoint is not None
        return remote_artifact_manifest(endpoint, remote_path=str(args["remote_path"]), timeout_ms=timeout_ms)
    if name == "remote.artifact_pull":
        assert endpoint is not None
        return remote_artifact_pull(endpoint, remote_path=str(args["remote_path"]), local_dir=args.get("local_dir"), timeout_ms=timeout_ms)
    if name == "remote.artifact_push":
        assert endpoint is not None
        return remote_artifact_push(endpoint, local_path=str(args["local_path"]), remote_path=str(args["remote_path"]), timeout_ms=timeout_ms)
    if name == "remote.context_snapshot":
        assert endpoint is not None
        return remote_context_snapshot(endpoint, timeout_ms=timeout_ms, live_probe=bool(args.get("live_probe", True)))
    if name == "remote.probe":
        assert endpoint is not None
        return remote_probe(endpoint, timeout_ms=timeout_ms)
    raise KeyError(f"unknown remote-dev tool: {name}")
