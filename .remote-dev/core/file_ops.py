from __future__ import annotations

import time
from typing import Any

from .endpoint import Endpoint
from .errors import PathPolicyError
from .path_policy import assert_under_root, join_under_root
from .preview import MAX_LINE_CHARS, MAX_READ_LINES, compact_text
from .result import make_result, new_invocation_id, utc_now_iso
from .ssh_transport import run_remote_python
from .state_store import load_read_ledger, resolve_ledger_scope, write_read_ledger

REMOTE_FILE_PY = r'''
import difflib
import hashlib
import json
import os
import pathlib
import sys
import tempfile

payload = json.loads(sys.stdin.read())
op = payload["op"]
root = pathlib.Path(payload["root"]).resolve()
cwd = pathlib.Path(payload.get("cwd") or payload["root"])

def fail(status, error=None, **extra):
    data = {"status": status}
    if error:
        data["error"] = error
    data.update(extra)
    print(json.dumps(data, sort_keys=True))
    raise SystemExit(0)

def resolve_path(raw, *, existing_parent_ok=False):
    if not raw:
        fail("path_required", "path is required")
    p = pathlib.Path(raw)
    if not p.is_absolute():
        p = cwd / p
    try:
        if p.exists() or p.is_symlink():
            resolved = p.resolve()
        elif existing_parent_ok:
            resolved = p.parent.resolve() / p.name
        else:
            resolved = p.resolve(strict=True)
    except FileNotFoundError:
        fail("not_found", f"remote path does not exist: {p}")
    if resolved != root and root not in resolved.parents:
        fail("path_outside_root", f"remote path is outside root: {resolved} not under {root}")
    return p, resolved

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()

def file_info(path, *, content=None):
    st = path.stat()
    data = path.read_bytes() if content is None else content
    return {
        "path": str(path),
        "resolved_path": str(path.resolve()),
        "sha256": sha256_bytes(data),
        "size": len(data),
        "mtime_ns": st.st_mtime_ns,
    }

def atomic_write(path, data):
    if path.is_symlink():
        fail("symlink_not_allowed", f"refusing to write symlink: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass

def unified(before, after, path):
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path.name}",
        tofile=f"b/{path.name}",
        n=3,
    ))[:12000]

if op == "read":
    path, resolved = resolve_path(payload["file_path"])
    if path.is_dir():
        fail("is_directory", f"RemoteRead reads files, not directories: {path}")
    if path.is_symlink() and not payload.get("allow_symlink", False):
        target = path.resolve()
        if target != root and root not in target.parents:
            fail("path_outside_root", f"symlink target escapes root: {target}")
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    offset = int(payload.get("offset") or 1)
    limit = int(payload.get("limit") or 200)
    if offset < 1 or limit < 1:
        fail("invalid_pagination", "offset and limit must be positive integers")
    start = min(offset - 1, len(lines))
    end = min(start + limit, len(lines))
    max_line_chars = int(payload.get("max_line_chars") or 2000)
    truncated_lines = 0
    formatted_lines = []
    for idx, line in enumerate(lines[start:end], start=start + 1):
        if len(line) > max_line_chars:
            line = line[:max_line_chars] + "<remote-dev line truncated>"
            truncated_lines += 1
        formatted_lines.append(f"{idx} | {line}")
    numbered = "\n".join(formatted_lines)
    info = file_info(path, content=raw)
    info.update({
        "total_lines": len(lines),
        "offset": offset,
        "limit": limit,
        "line_start": start + 1 if lines else 0,
        "line_end": end,
        "partial": start > 0 or end < len(lines),
        "content": numbered,
        "truncated_line_count": truncated_lines,
        "symlink": path.is_symlink(),
        "resolved_path": str(resolved),
    })
    warnings = []
    if truncated_lines:
        warnings.append(f"{truncated_lines} line(s) truncated to {max_line_chars} chars")
    print(json.dumps({"status": "partial" if info["partial"] else "ok", "file": info, "warnings": warnings}, sort_keys=True))
    raise SystemExit(0)

if op == "ls":
    path, resolved = resolve_path(payload["path"])
    if not path.is_dir():
        fail("not_directory", f"RemoteLS lists directories, not files: {path}")
    limit = int(payload.get("limit") or 200)
    show_all = bool(payload.get("all", False))
    entries = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        if not show_all and child.name.startswith("."):
            continue
        try:
            st = child.lstat()
            if child.is_symlink():
                kind = "symlink"
            elif child.is_dir():
                kind = "directory"
            elif child.is_file():
                kind = "file"
            else:
                kind = "other"
            entries.append({
                "name": child.name,
                "path": str(child),
                "type": kind,
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "is_symlink": child.is_symlink(),
            })
        except OSError as exc:
            entries.append({"name": child.name, "path": str(child), "type": "error", "error": str(exc)})
    truncated = len(entries) > limit
    print(json.dumps({"status": "ok", "path": str(path), "resolved_path": str(resolved), "entries": entries[:limit], "truncated": truncated}, sort_keys=True))
    raise SystemExit(0)

if op == "write":
    path, resolved = resolve_path(payload["file_path"], existing_parent_ok=True)
    content = payload.get("content", "")
    raw = content.encode("utf-8")
    create_dirs = bool(payload.get("create_dirs", False))
    overwrite = bool(payload.get("overwrite", False))
    existed = path.exists() or path.is_symlink()
    if existed and path.is_symlink():
        fail("symlink_not_allowed", f"refusing to overwrite symlink: {path}")
    if existed and not path.is_file():
        fail("not_file", f"refusing to overwrite non-file path: {path}")
    if existed and not overwrite:
        fail("file_exists", f"remote file already exists: {path}")
    before = path.read_bytes() if existed else None
    before_sha = sha256_bytes(before) if before is not None else None
    expected_sha = payload.get("expected_sha256")
    if existed and expected_sha and before_sha != expected_sha:
        fail("file_changed_since_read", "remote file changed since last RemoteRead", before_sha256=before_sha, expected_sha256=expected_sha)
    if not path.parent.exists():
        if create_dirs:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            fail("parent_not_found", f"parent directory does not exist: {path.parent}")
    atomic_write(path, raw)
    after_info = file_info(path)
    before_text = before.decode("utf-8", errors="replace") if before is not None else ""
    after_text = raw.decode("utf-8", errors="replace")
    print(json.dumps({
        "status": "written",
        "file": after_info,
        "before_sha256": before_sha,
        "after_sha256": after_info["sha256"],
        "diff_preview": unified(before_text, after_text, path),
    }, sort_keys=True))
    raise SystemExit(0)

if op in {"edit", "multi_edit"}:
    path, resolved = resolve_path(payload["file_path"])
    if path.is_symlink():
        fail("symlink_not_allowed", f"refusing to edit symlink: {path}")
    if not path.is_file():
        fail("not_file", f"remote path is not a file: {path}")
    before_raw = path.read_bytes()
    before_sha = sha256_bytes(before_raw)
    expected_sha = payload.get("expected_sha256")
    if expected_sha and before_sha != expected_sha:
        fail("file_changed_since_read", "remote file changed since last RemoteRead", before_sha256=before_sha, expected_sha256=expected_sha)
    before = before_raw.decode("utf-8", errors="replace")
    after = before
    edits = payload.get("edits") if op == "multi_edit" else [{
        "old_string": payload.get("old_string", ""),
        "new_string": payload.get("new_string", ""),
        "replace_all": bool(payload.get("replace_all", False)),
    }]
    if not isinstance(edits, list) or not edits:
        fail("invalid_edits", "edits must be a non-empty list")
    for index, edit in enumerate(edits):
        old = edit.get("old_string", "")
        new = edit.get("new_string", "")
        replace_all = bool(edit.get("replace_all", False))
        if old == "":
            fail("empty_old_string", f"edit {index} has an empty old_string")
        count = after.count(old)
        if count == 0:
            fail("old_string_not_found", f"edit {index} old_string was not found")
        if count > 1 and not replace_all:
            fail("old_string_not_unique", f"edit {index} old_string matched {count} times")
        after = after.replace(old, new if replace_all else new, -1 if replace_all else 1)
    if after == before:
        fail("no_change", "edit produced no file changes")
    atomic_write(path, after.encode("utf-8"))
    after_info = file_info(path)
    print(json.dumps({
        "status": "edited",
        "file": after_info,
        "before_sha256": before_sha,
        "after_sha256": after_info["sha256"],
        "diff_preview": unified(before, after, path),
    }, sort_keys=True))
    raise SystemExit(0)

fail("unsupported_op", f"unsupported file op: {op}")
'''


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


def _status_to_outcome(status: str) -> str:
    if status in {"ok", "partial", "written", "edited"}:
        return "success"
    if status in {"read_required", "file_changed_since_read", "old_string_not_unique", "path_outside_root", "symlink_not_allowed", "file_exists"}:
        return "blocked"
    if status in {"path_required", "invalid_pagination", "parent_not_found"}:
        return "needs_input"
    return "failed"


def remote_read(
    endpoint: Endpoint,
    *,
    file_path: str,
    offset: int = 1,
    limit: int = 200,
    allow_symlink: bool = False,
    client_context_id: str | None = None,
    timeout_ms: int = 120000,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    warnings = []
    if limit > MAX_READ_LINES:
        warnings.append(f"limit clamped from {limit} to {MAX_READ_LINES}")
        limit = MAX_READ_LINES
    if limit < 1:
        limit = 1
    try:
        path = join_under_root(endpoint.root, endpoint.effective_cwd, file_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.read", file_path, str(exc), started, start)
    data = run_remote_python(
        endpoint,
        REMOTE_FILE_PY,
        {
            "op": "read",
            "root": endpoint.root,
            "cwd": endpoint.effective_cwd,
            "file_path": path,
            "offset": offset,
            "limit": limit,
            "allow_symlink": allow_symlink,
            "max_line_chars": MAX_LINE_CHARS,
        },
        timeout_ms=timeout_ms,
    )
    status = str(data.get("status", "failed"))
    refs: dict[str, Any] = {}
    ledger_scope = resolve_ledger_scope(client_context_id)
    if status in {"ok", "partial"} and isinstance(data.get("file"), dict):
        ledger = write_read_ledger(endpoint, data["file"], client_context_id)
        refs["read_ledger"] = str(ledger)
    file_info = data.get("file", {}) if isinstance(data.get("file"), dict) else {}
    warnings.extend(data.get("warnings", []) if isinstance(data.get("warnings"), list) else [])
    result = make_result(
        tool="remote.read",
        target=endpoint.to_result_target(),
        outcome=_status_to_outcome(status),
        status=status,
        summary=f"RemoteRead {status} for {path}",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"content": compact_text(str(file_info.get("content", ""))), "partial": file_info.get("partial", False)},
        refs=refs,
        warnings=warnings,
        extra={"file": {k: v for k, v in file_info.items() if k != "content"}, "error": data.get("error"), "ledger_scope": ledger_scope},
    )
    text = _format_read_text(endpoint, result, file_info)
    return {"text": text, "result": result}


def remote_ls(
    endpoint: Endpoint,
    *,
    path: str | None = None,
    limit: int = 200,
    all: bool = False,
    timeout_ms: int = 120000,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    raw_path = path or endpoint.effective_cwd
    try:
        resolved = join_under_root(endpoint.root, endpoint.effective_cwd, raw_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.ls", raw_path, str(exc), started, start)
    data = run_remote_python(
        endpoint,
        REMOTE_FILE_PY,
        {"op": "ls", "root": endpoint.root, "cwd": endpoint.effective_cwd, "path": resolved, "limit": limit, "all": all},
        timeout_ms=timeout_ms,
    )
    status = str(data.get("status", "failed"))
    entries = data.get("entries", []) if isinstance(data.get("entries"), list) else []
    result = make_result(
        tool="remote.ls",
        target=endpoint.to_result_target(),
        outcome=_status_to_outcome(status),
        status=status,
        summary=f"RemoteLS {status} for {resolved}",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"entries": entries, "truncated": bool(data.get("truncated", False))},
        extra={"path": data.get("path", resolved), "entries": entries, "truncated": bool(data.get("truncated", False)), "error": data.get("error")},
    )
    return {"text": _format_ls_text(endpoint, result), "result": result}


def remote_write(
    endpoint: Endpoint,
    *,
    file_path: str,
    content: str,
    overwrite: bool = False,
    create_dirs: bool = False,
    client_context_id: str | None = None,
    timeout_ms: int = 120000,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    try:
        path = join_under_root(endpoint.root, endpoint.effective_cwd, file_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.write", file_path, str(exc), started, start)
    ledger = load_read_ledger(endpoint, path, client_context_id)
    data = run_remote_python(
        endpoint,
        REMOTE_FILE_PY,
        {
            "op": "write",
            "root": endpoint.root,
            "cwd": endpoint.effective_cwd,
            "file_path": path,
            "content": content,
            "overwrite": overwrite,
            "create_dirs": create_dirs,
            "expected_sha256": ledger.get("sha256") if ledger else None,
        },
        timeout_ms=timeout_ms,
    )
    return _write_like_result(endpoint, "remote.write", path, data, started, start, client_context_id=client_context_id)


def remote_edit(
    endpoint: Endpoint,
    *,
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    client_context_id: str | None = None,
    timeout_ms: int = 120000,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    try:
        path = join_under_root(endpoint.root, endpoint.effective_cwd, file_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.edit", file_path, str(exc), started, start)
    ledger = load_read_ledger(endpoint, path, client_context_id)
    data = run_remote_python(
        endpoint,
        REMOTE_FILE_PY,
        {
            "op": "edit",
            "root": endpoint.root,
            "cwd": endpoint.effective_cwd,
            "file_path": path,
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
            "expected_sha256": ledger.get("sha256") if ledger else None,
        },
        timeout_ms=timeout_ms,
    )
    return _write_like_result(endpoint, "remote.edit", path, data, started, start, client_context_id=client_context_id)


def remote_multi_edit(
    endpoint: Endpoint,
    *,
    file_path: str,
    edits: list[dict[str, Any]],
    client_context_id: str | None = None,
    timeout_ms: int = 120000,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    try:
        path = join_under_root(endpoint.root, endpoint.effective_cwd, file_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.multi_edit", file_path, str(exc), started, start)
    ledger = load_read_ledger(endpoint, path, client_context_id)
    data = run_remote_python(
        endpoint,
        REMOTE_FILE_PY,
        {
            "op": "multi_edit",
            "root": endpoint.root,
            "cwd": endpoint.effective_cwd,
            "file_path": path,
            "edits": edits,
            "expected_sha256": ledger.get("sha256") if ledger else None,
        },
        timeout_ms=timeout_ms,
    )
    return _write_like_result(endpoint, "remote.multi_edit", path, data, started, start, client_context_id=client_context_id)


def _write_like_result(
    endpoint: Endpoint,
    tool: str,
    path: str,
    data: dict[str, Any],
    started: str,
    start: float,
    *,
    client_context_id: str | None = None,
) -> dict[str, Any]:
    status = str(data.get("status", "failed"))
    file_info = data.get("file", {}) if isinstance(data.get("file"), dict) else {}
    refs: dict[str, Any] = {}
    ledger_scope = resolve_ledger_scope(client_context_id)
    if status in {"written", "edited"} and file_info:
        refs["read_ledger"] = str(write_read_ledger(endpoint, file_info, client_context_id))
    changed = []
    if file_info:
        changed.append({
            "path": file_info.get("path", path),
            "before_sha256": data.get("before_sha256"),
            "after_sha256": data.get("after_sha256") or file_info.get("sha256"),
            "size": file_info.get("size"),
        })
    result = make_result(
        tool=tool,
        target=endpoint.to_result_target(),
        outcome=_status_to_outcome(status),
        status=status,
        summary=f"{tool} {status} for {path}",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"diff": data.get("diff_preview", "")},
        refs=refs,
        changed_files=changed,
        extra={"file": file_info, "error": data.get("error"), "ledger_scope": ledger_scope},
    )
    return {"text": _format_write_text(endpoint, result, data), "result": result}


def _path_blocked_result(
    endpoint: Endpoint,
    tool: str,
    path: str,
    error: str,
    started: str,
    start: float,
) -> dict[str, Any]:
    result = make_result(
        tool=tool,
        target=endpoint.to_result_target(),
        outcome="blocked",
        status="path_outside_root",
        summary=f"{tool} blocked for {path}",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"stderr": error},
        extra={"error": error},
    )
    return {"text": result["summary"] + "\n" + error + "\n", "result": result}


def _format_endpoint(endpoint: Endpoint) -> str:
    return f"{endpoint.user}@{endpoint.host}:{endpoint.port}"


def _format_read_text(endpoint: Endpoint, result: dict[str, Any], file_info: dict[str, Any]) -> str:
    lines = [
        f"RemoteRead on {_format_endpoint(endpoint)}",
        f"file: {file_info.get('path', '')}",
        f"lines: {file_info.get('line_start', 0)}-{file_info.get('line_end', 0)} of {file_info.get('total_lines', 0)}",
        f"partial: {str(file_info.get('partial', False)).lower()}",
        "",
        str(result.get("preview", {}).get("content", "")),
    ]
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    return compact_text("\n".join(lines).rstrip() + "\n")


def _format_ls_text(endpoint: Endpoint, result: dict[str, Any]) -> str:
    entries = result.get("entries", [])
    lines = [f"RemoteLS on {_format_endpoint(endpoint)}", f"path: {result.get('path')}", ""]
    for entry in entries:
        lines.append(f"{entry.get('type', 'unknown'):10} {entry.get('size', 0):>10} {entry.get('name', '')}")
    if result.get("truncated"):
        lines.append("<truncated>")
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    return compact_text("\n".join(lines).rstrip() + "\n")


def _format_write_text(endpoint: Endpoint, result: dict[str, Any], data: dict[str, Any]) -> str:
    lines = [
        f"{result['tool']} {result['status']} on {_format_endpoint(endpoint)}",
        f"file: {data.get('file', {}).get('path', '')}",
        "",
    ]
    if result.get("changed_files"):
        lines.append("Changed:")
        for item in result["changed_files"]:
            lines.append(f"  M {item.get('path')}")
        lines.extend(["", f"Before sha256: {data.get('before_sha256')}", f"After sha256:  {data.get('after_sha256')}", ""])
    if data.get("diff_preview"):
        lines.append("Diff preview:")
        lines.append(str(data["diff_preview"]))
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    return compact_text("\n".join(lines).rstrip() + "\n")
