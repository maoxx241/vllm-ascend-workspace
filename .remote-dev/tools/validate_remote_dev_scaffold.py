#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


REMOTE_DEV_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = REMOTE_DEV_ROOT.parent
if str(REMOTE_DEV_ROOT) not in sys.path:
    sys.path.insert(0, str(REMOTE_DEV_ROOT))

from mcp.schemas import ENDPOINT_PROPS, ENDPOINT_SELECTOR_ANY_OF, TOOL_SCHEMAS  # noqa: E402
from mcp.tools import call_tool, list_resources, list_tools, read_resource  # noqa: E402


def progress(message: str) -> None:
    print(f"__REMOTE_DEV_VALIDATE_PROGRESS__={message}", file=sys.stderr, flush=True)


def run_command(name: str, argv: list[str]) -> dict[str, Any]:
    started = time.monotonic()
    proc = subprocess.run(
        argv,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "name": name,
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }


def local_checks() -> list[dict[str, Any]]:
    commands = [
        ("compileall", ["python3", "-m", "compileall", "-q", ".remote-dev", ".agents"]),
        ("remote_dev_unittest", ["python3", "-m", "unittest", "discover", "-s", ".remote-dev/tests"]),
        ("agents_unittest", ["python3", "-m", "unittest", "discover", "-s", ".agents/tests"]),
        ("claude_skill_shims", ["python3", ".remote-dev/tools/sync_claude_skills.py", "--check"]),
        (
            "diff_check",
            [
                "git",
                "diff",
                "--check",
                "--",
                ".remote-dev",
                ".agents",
                "AGENTS.md",
                "CLAUDE.md",
                ".mcp.json",
                ".codex",
                ".claude",
                ".gitignore",
            ],
        ),
    ]
    results = []
    for name, argv in commands:
        progress(f"local:{name}")
        results.append(run_command(name, argv))
    return results


def mcp_and_burden_checks() -> dict[str, Any]:
    tools = list_tools()
    names = [tool["name"] for tool in tools]
    scripts = sorted((REMOTE_DEV_ROOT / "tools").glob("remote_*.py"))
    expected_scripts = {REMOTE_DEV_ROOT / "tools" / (name.replace(".", "_") + ".py") for name in TOOL_SCHEMAS}
    endpoint_fields = set(ENDPOINT_PROPS)
    required_by_tool = {name: set(schema.get("required", [])) for name, schema in TOOL_SCHEMAS.items()}
    endpoint_required = {
        name: sorted(required & endpoint_fields)
        for name, required in required_by_tool.items()
        if required & endpoint_fields
    }
    endpoint_selector_tools = {
        name
        for name, schema in TOOL_SCHEMAS.items()
        if {"anyOf": ENDPOINT_SELECTOR_ANY_OF} in schema.get("allOf", [])
    }
    endpoint_selector_missing = sorted(set(TOOL_SCHEMAS) - {"remote.job_status", "remote.job_tail", "remote.job_stop"} - endpoint_selector_tools)
    own_required_counts = {
        name: len(required - endpoint_fields)
        for name, required in required_by_tool.items()
    }
    has_native_shape_names = all(name.startswith("remote.") for name in names)
    all_have_schema = all("inputSchema" in tool for tool in tools)
    resources = list_resources()
    status = "ok"
    failures: list[str] = []
    if set(names) != set(TOOL_SCHEMAS):
        failures.append("tools/list does not match TOOL_SCHEMAS")
    if set(scripts) != expected_scripts:
        failures.append("CLI remote_*.py wrappers do not match TOOL_SCHEMAS")
    if endpoint_required:
        failures.append("endpoint fields should not be top-level required by tool schemas")
    if endpoint_selector_missing:
        failures.append("remote tools should express endpoint selector anyOf in tool schemas")
    if not has_native_shape_names:
        failures.append("tool names should retain remote.<native-tool> shape")
    if not all_have_schema:
        failures.append("every tool must expose inputSchema")
    if failures:
        status = "failed"
    return {
        "status": status,
        "tool_count": len(tools),
        "tools": names,
        "resource_count": len(resources),
        "cli_wrapper_count": len(scripts),
        "endpoint_required": endpoint_required,
        "endpoint_selector_missing": endpoint_selector_missing,
        "own_required_counts": own_required_counts,
        "max_own_required_fields": max(own_required_counts.values()) if own_required_counts else 0,
        "failures": failures,
    }


def require_outcome(name: str, payload: dict[str, Any], *, statuses: set[str] | None = None, outcomes: set[str] | None = None) -> dict[str, Any]:
    result = payload.get("result", {})
    outcome = str(result.get("outcome"))
    status = str(result.get("status"))
    if outcomes is None:
        outcomes = {"success"}
    if outcome not in outcomes or (statuses is not None and status not in statuses):
        raise RuntimeError(f"{name} returned outcome={outcome} status={status}: {payload.get('text', '')[:1000]}")
    return {
        "name": name,
        "outcome": outcome,
        "status": status,
        "duration_ms": result.get("duration_ms"),
        "summary": result.get("summary"),
    }


def endpoint_payload(args: argparse.Namespace) -> dict[str, Any]:
    cwd = args.cwd
    if cwd is None and args.root != "/":
        cwd = args.root
    payload = {
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "root": args.root,
        "cwd": cwd,
        "connect_timeout_ms": args.connect_timeout_ms,
        "alias": args.alias,
        "session_id": args.session_id,
        "session_file": args.session_file,
        "machine": args.machine,
    }
    return {key: value for key, value in payload.items() if value is not None}


def run_parallel_worker(endpoint: dict[str, Any], scratch: str, index: int, timeout_ms: int) -> dict[str, Any]:
    worker_dir = f"{scratch}/parallel-{index}"
    file_path = f"{worker_dir}/task.txt"
    command = f"mkdir -p {worker_dir!r} && printf 'worker-{index}\\ninitial\\n' > {file_path!r}"
    checks = [
        require_outcome(f"parallel_{index}_create", call_tool("remote.bash", {**endpoint, "command": command, "timeout_ms": timeout_ms})),
        require_outcome(f"parallel_{index}_read", call_tool("remote.read", {**endpoint, "file_path": file_path, "timeout_ms": timeout_ms}), statuses={"ok"}),
        require_outcome(
            f"parallel_{index}_edit",
            call_tool(
                "remote.edit",
                {
                    **endpoint,
                    "file_path": file_path,
                    "old_string": "initial",
                    "new_string": f"done-{index}",
                    "timeout_ms": timeout_ms,
                },
            ),
            statuses={"edited"},
        ),
    ]
    return {"worker": index, "status": "ok", "checks": checks}


def live_endpoint_checks(args: argparse.Namespace) -> dict[str, Any]:
    endpoint = endpoint_payload(args)
    if not any(endpoint.get(key) for key in ("host", "alias", "session_id", "session_file", "machine")):
        return {"status": "skipped", "reason": "no endpoint selector was provided"}
    timeout_ms = args.timeout_ms
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    scratch_root = (endpoint.get("cwd") or "/vllm-workspace").rstrip("/")
    scratch = f"{scratch_root}/.remote-dev/validation/{stamp}"
    narrow_endpoint = {**endpoint, "root": scratch_root, "cwd": scratch_root}
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    try:
        progress("remote:probe")
        checks.append(require_outcome("probe", call_tool("remote.probe", {**endpoint, "timeout_ms": timeout_ms})))
        checks.append(require_outcome("context_snapshot", call_tool("remote.context_snapshot", {**endpoint, "timeout_ms": timeout_ms, "live_probe": True})))
        checks.append(require_outcome("cwd_blocked", call_tool("remote.bash", {**narrow_endpoint, "cwd": "/tmp", "command": "pwd", "timeout_ms": timeout_ms}), outcomes={"blocked"}, statuses={"cwd_outside_root"}))
        checks.append(require_outcome("cwd_not_found", call_tool("remote.bash", {**endpoint, "cwd": f"{scratch}/missing", "command": "pwd", "timeout_ms": timeout_ms}), outcomes={"failed"}, statuses={"cwd_not_found"}))
        checks.append(require_outcome("nonzero_exit", call_tool("remote.bash", {**endpoint, "command": "exit 7", "timeout_ms": timeout_ms}), outcomes={"failed"}, statuses={"nonzero_exit"}))
        checks.append(require_outcome("timeout", call_tool("remote.bash", {**endpoint, "command": "sleep 2", "timeout_ms": 500}), outcomes={"timeout"}, statuses={"timeout"}))

        setup = f"mkdir -p {scratch!r} && printf 'alpha\\nbeta\\n' > {scratch!r}/file.txt && ln -sf /etc/passwd {scratch!r}/escape-link"
        checks.append(require_outcome("bash_create", call_tool("remote.bash", {**endpoint, "command": setup, "timeout_ms": timeout_ms})))
        big = call_tool("remote.bash", {**endpoint, "command": "python3 - <<'PY'\nprint('x' * 50000)\nPY", "timeout_ms": timeout_ms})
        checks.append(require_outcome("large_output_preview", big))
        if not big.get("result", {}).get("preview", {}).get("stdout", {}).get("truncated"):
            raise RuntimeError("large_output_preview did not mark stdout as truncated")
        checks.append(require_outcome("ls", call_tool("remote.ls", {**endpoint, "path": scratch, "timeout_ms": timeout_ms})))
        checks.append(require_outcome("read", call_tool("remote.read", {**endpoint, "file_path": f"{scratch}/file.txt", "offset": 1, "limit": 10, "timeout_ms": timeout_ms}), statuses={"ok"}))
        checks.append(require_outcome("directory_read_rejected", call_tool("remote.read", {**endpoint, "file_path": scratch, "timeout_ms": timeout_ms}), outcomes={"failed"}, statuses={"is_directory"}))
        checks.append(require_outcome("symlink_read_blocked", call_tool("remote.read", {**narrow_endpoint, "file_path": f"{scratch}/escape-link", "timeout_ms": timeout_ms}), outcomes={"blocked"}, statuses={"path_outside_root"}))
        checks.append(require_outcome("artifact_symlink_blocked", call_tool("remote.artifact_manifest", {**endpoint, "remote_path": f"{scratch}/escape-link", "timeout_ms": timeout_ms}), outcomes={"blocked"}))
        checks.append(require_outcome("remove_escape_symlink", call_tool("remote.bash", {**endpoint, "command": f"rm -f {scratch!r}/escape-link", "timeout_ms": timeout_ms})))
        checks.append(require_outcome("edit", call_tool("remote.edit", {**endpoint, "file_path": f"{scratch}/file.txt", "old_string": "beta", "new_string": "gamma", "timeout_ms": timeout_ms}), statuses={"edited"}))
        checks.append(require_outcome("read_after_edit", call_tool("remote.read", {**endpoint, "file_path": f"{scratch}/file.txt", "timeout_ms": timeout_ms}), statuses={"ok"}))
        checks.append(require_outcome("write", call_tool("remote.write", {**endpoint, "file_path": f"{scratch}/write.txt", "content": "created\\n", "create_dirs": True, "timeout_ms": timeout_ms}), statuses={"written"}))
        checks.append(require_outcome("glob", call_tool("remote.glob", {**endpoint, "pattern": "*.txt", "path": scratch, "timeout_ms": timeout_ms})))
        checks.append(require_outcome("grep_content", call_tool("remote.grep", {**endpoint, "pattern": "gamma", "path": scratch, "glob": "*.txt", "output_mode": "content", "timeout_ms": timeout_ms})))
        patch = f"""*** Begin Patch
*** Add File: {scratch}/patch-old.txt
+old
*** Update File: {scratch}/patch-old.txt
*** Move to: {scratch}/patch-new.txt
@@
-old
+new
*** End of File
*** End Patch
"""
        checks.append(require_outcome("apply_patch", call_tool("remote.apply_patch", {**endpoint, "patch": patch, "timeout_ms": timeout_ms}), statuses={"applied"}))
        checks.append(require_outcome("artifact_manifest", call_tool("remote.artifact_manifest", {**endpoint, "remote_path": scratch, "timeout_ms": timeout_ms}), statuses={"ok"}))
        with tempfile.TemporaryDirectory() as tmp:
            checks.append(require_outcome("artifact_pull", call_tool("remote.artifact_pull", {**endpoint, "remote_path": f"{scratch}/file.txt", "local_dir": tmp, "timeout_ms": timeout_ms}), statuses={"ok"}))
            local_push = Path(tmp) / "push.txt"
            local_push.write_text("pushed\n", encoding="utf-8")
            checks.append(require_outcome("artifact_push", call_tool("remote.artifact_push", {**endpoint, "local_path": str(local_push), "remote_path": f"{scratch}/pushed.txt", "timeout_ms": timeout_ms}), statuses={"ok"}))

        job_payload = call_tool("remote.bash", {**endpoint, "command": "printf 'job-out\\n'; printf 'job-err\\n' >&2", "cwd": scratch, "run_in_background": True, "timeout_ms": timeout_ms})
        checks.append(require_outcome("background_job_start", job_payload, statuses={"running"}))
        job_id = job_payload["result"].get("job", job_payload["result"].get("extra", {}).get("job", {}))["job_id"]
        time.sleep(2)
        checks.append(require_outcome("job_status", call_tool("remote.job_status", {**endpoint, "job_id": job_id, "timeout_ms": timeout_ms}), statuses={"succeeded"}))
        checks.append(require_outcome("job_tail", call_tool("remote.job_tail", {**endpoint, "job_id": job_id, "lines": 20, "timeout_ms": timeout_ms})))
        resource_uris = {item["uri"] for item in list_resources()}
        stdout_uri = next((uri for uri in resource_uris if uri.endswith(f"/job/{job_id}/stdout")), None)
        if not stdout_uri:
            raise RuntimeError(f"MCP job stdout resource missing for {job_id}")
        stdout_resource = read_resource(stdout_uri)
        if "job-out" not in stdout_resource.get("text", ""):
            raise RuntimeError("MCP job stdout resource did not include remote log content")
        checks.append({"name": "mcp_job_stdout_resource", "outcome": "success", "status": "ok"})
        artifact_resource_count = len([uri for uri in resource_uris if "/artifacts/" in uri and uri.endswith("/manifest")])
        if artifact_resource_count < 1:
            raise RuntimeError("MCP artifact manifest resource was not registered")
        checks.append({"name": "mcp_artifact_manifest_resource", "outcome": "success", "status": "ok", "count": artifact_resource_count})

        progress("remote:parallel_workers")
        parallel_results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel_workers) as executor:
            futures = [executor.submit(run_parallel_worker, endpoint, scratch, index, timeout_ms) for index in range(args.parallel_workers)]
            for future in concurrent.futures.as_completed(futures):
                parallel_results.append(future.result())
        checks.append({"name": "parallel_workers", "outcome": "success", "status": "ok", "workers": sorted(parallel_results, key=lambda item: item["worker"])})
    except Exception as exc:  # noqa: BLE001
        failures.append(str(exc))
    finally:
        cleanup = call_tool("remote.bash", {**endpoint, "command": f"rm -rf {scratch!r}", "timeout_ms": timeout_ms})
        checks.append({
            "name": "cleanup",
            "outcome": cleanup.get("result", {}).get("outcome"),
            "status": cleanup.get("result", {}).get("status"),
        })
    return {
        "status": "ok" if not failures else "failed",
        "target": endpoint,
        "scratch": scratch,
        "parallel_workers": args.parallel_workers,
        "checks": checks,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the remote-dev scaffold contract and optional live endpoint behavior.")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--user", default="root")
    parser.add_argument("--root", default="/")
    parser.add_argument("--cwd")
    parser.add_argument("--connect-timeout-ms", type=int, default=10000)
    parser.add_argument("--alias")
    parser.add_argument("--session-id")
    parser.add_argument("--session-file")
    parser.add_argument("--machine")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--parallel-workers", type=int, default=3)
    parser.add_argument("--skip-local", action="store_true")
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args()
    if args.parallel_workers < 1:
        parser.error("--parallel-workers must be >= 1")

    report: dict[str, Any] = {
        "schema_version": "remote-dev.validation.v1",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "local_checks": [],
        "mcp_and_burden": {},
        "live_endpoint": {},
    }
    if not args.skip_local:
        report["local_checks"] = local_checks()
    progress("local:mcp_and_burden")
    report["mcp_and_burden"] = mcp_and_burden_checks()
    if not args.local_only:
        report["live_endpoint"] = live_endpoint_checks(args)
    else:
        report["live_endpoint"] = {"status": "skipped", "reason": "--local-only"}

    failed = False
    failed = failed or any(item.get("status") != "ok" for item in report["local_checks"])
    failed = failed or report["mcp_and_burden"].get("status") != "ok"
    failed = failed or report["live_endpoint"].get("status") == "failed"
    report["status"] = "failed" if failed else "ok"
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
