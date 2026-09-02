from __future__ import annotations

import time
from typing import Any

from .endpoint import Endpoint
from .errors import PathPolicyError
from .path_policy import join_under_root
from .preview import MAX_GREP_MATCHES, MAX_LINE_CHARS, MAX_TEXT_CHARS, compact_text
from .result import make_result, utc_now_iso
from .ssh_transport import run_remote_python

REMOTE_SEARCH_PY = r'''
import fnmatch
import glob as glob_mod
import json
import os
import pathlib
import shutil
import subprocess
import sys

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

def resolve_path(raw):
    p = pathlib.Path(raw)
    if not p.is_absolute():
        p = cwd / p
    try:
        resolved = p.resolve()
    except FileNotFoundError:
        fail("not_found", f"remote path does not exist: {p}")
    if resolved != root and root not in resolved.parents:
        fail("path_outside_root", f"remote path is outside root: {resolved} not under {root}")
    return p, resolved

if op == "glob":
    base, resolved = resolve_path(payload.get("path") or payload["root"])
    if not base.is_dir():
        fail("not_directory", f"RemoteGlob path is not a directory: {base}")
    pattern = payload.get("pattern") or "*"
    limit = int(payload.get("limit") or 100)
    matches = []
    for item in glob_mod.glob(pattern, root_dir=str(base), recursive=True):
        path = base / item
        try:
            st = path.lstat()
        except OSError:
            continue
        matches.append({"path": str(path), "relpath": item, "type": "directory" if path.is_dir() else "file", "mtime_ns": st.st_mtime_ns, "size": st.st_size})
    matches.sort(key=lambda row: row["mtime_ns"], reverse=True)
    print(json.dumps({"status": "ok", "matches": matches[:limit], "truncated": len(matches) > limit}, sort_keys=True))
    raise SystemExit(0)

if op == "grep":
    base, resolved = resolve_path(payload.get("path") or payload["root"])
    if not base.exists():
        fail("not_found", f"RemoteGrep path does not exist: {base}")
    pattern = payload.get("pattern")
    if not pattern:
        fail("pattern_required", "RemoteGrep requires pattern")
    limit = int(payload.get("limit") or 100)
    max_line_chars = int(payload.get("max_line_chars") or 2000)
    output_mode = payload.get("output_mode") or "files_with_matches"
    glob_pattern = payload.get("glob")
    type_name = payload.get("type")
    multiline = bool(payload.get("multiline", False))
    warnings = []
    rg_path = shutil.which("rg")
    if rg_path:
        cmd = [rg_path, "--color", "never"]
        if multiline:
            cmd.append("-U")
        if glob_pattern:
            cmd.extend(["--glob", glob_pattern])
        if type_name:
            cmd.extend(["--type", type_name])
        if output_mode == "files_with_matches":
            cmd.append("-l")
        elif output_mode == "count":
            cmd.append("-c")
        else:
            cmd.append("-n")
        cmd.extend([pattern, str(base)])
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode not in (0, 1):
            fail("failed", proc.stderr[-4000:])
        lines = proc.stdout.splitlines()
        truncated_line_count = 0
        if output_mode == "content":
            capped = []
            for line in lines:
                if len(line) > max_line_chars:
                    line = line[:max_line_chars] + "<remote-dev line truncated>"
                    truncated_line_count += 1
                capped.append(line)
            lines = capped
        print(json.dumps({
            "status": "ok",
            "engine": "rg",
            "output_mode": output_mode,
            "matches": lines[:limit],
            "truncated": len(lines) > limit,
            "warnings": warnings + ([f"{truncated_line_count} line(s) truncated to {max_line_chars} chars"] if truncated_line_count else []),
        }, sort_keys=True))
        raise SystemExit(0)

    # rg is unavailable: fall back to POSIX `grep -E`, which preserves regex
    # semantics. Never silently degrade to substring matching, and fail fast on
    # features grep cannot honor instead of returning semantically wrong "ok".
    if multiline:
        fail("rg_required", "multiline grep requires ripgrep (rg) on the remote host; install rg or drop multiline")
    if type_name:
        fail("rg_required", f"grep fallback cannot honor --type {type_name}; install ripgrep (rg) or use --glob")
    if glob_pattern and "/" in glob_pattern:
        # grep --include matches file basenames only; a path-anchored glob
        # like "src/**/*.py" cannot be honored faithfully.
        fail("rg_required", f"grep fallback cannot honor path-anchored --glob {glob_pattern}; install ripgrep (rg) or use a basename glob")
    grep_path = shutil.which("grep")
    if not grep_path:
        fail("grep_unavailable", "neither rg nor grep found on the remote host")
    warnings.append("rg not found; used grep -E fallback (POSIX ERE semantics)")
    cmd = [grep_path, "-r", "-E", "-I"]
    # Align with rg defaults, which skip .git and hidden directories while
    # descending. grep applies --exclude-dir to the base operand itself, so
    # skip a pattern that matches the explicitly requested base (rg searches
    # an explicitly named hidden or .git path).
    for exclude_dir in (".git", ".*"):
        if fnmatch.fnmatch(base.name, exclude_dir):
            continue
        cmd.append(f"--exclude-dir={exclude_dir}")
    if glob_pattern:
        cmd.append(f"--include={glob_pattern}")
    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    else:
        cmd.append("-n")
    cmd.extend(["--", pattern, str(base)])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode not in (0, 1):
        fail("failed", proc.stderr[-4000:])
    lines = proc.stdout.splitlines()
    if output_mode == "count":
        # Match rg -c behavior: only report files with at least one match.
        lines = [line for line in lines if not line.endswith(":0")]
    truncated_line_count = 0
    if output_mode == "content":
        capped = []
        for line in lines:
            if len(line) > max_line_chars:
                line = line[:max_line_chars] + "<remote-dev line truncated>"
                truncated_line_count += 1
            capped.append(line)
        lines = capped
        if truncated_line_count:
            warnings.append(f"{truncated_line_count} line(s) truncated to {max_line_chars} chars")
    print(json.dumps({"status": "ok", "engine": "grep", "output_mode": output_mode, "matches": lines[:limit], "truncated": len(lines) > limit, "warnings": warnings}, sort_keys=True))
    raise SystemExit(0)

fail("unsupported_op", f"unsupported search op: {op}")
'''


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


def _compact_matches(matches: list[Any]) -> tuple[list[str], bool]:
    visible: list[str] = []
    total = 0
    truncated = False
    for item in matches:
        text = str(item)
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + "<remote-dev line truncated>"
            truncated = True
        if total + len(text) > MAX_TEXT_CHARS:
            truncated = True
            break
        visible.append(text)
        total += len(text)
    return visible, truncated or len(visible) < len(matches)


def remote_glob(
    endpoint: Endpoint,
    *,
    pattern: str,
    path: str | None = None,
    limit: int = 100,
    respect_gitignore: bool = False,
    timeout_ms: int = 120000,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    raw_path = path or endpoint.effective_cwd
    try:
        base = join_under_root(endpoint.root, endpoint.effective_cwd, raw_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.glob", raw_path, str(exc), started, start)
    data = run_remote_python(
        endpoint,
        REMOTE_SEARCH_PY,
        {
            "op": "glob",
            "root": endpoint.root,
            "cwd": endpoint.effective_cwd,
            "path": base,
            "pattern": pattern,
            "limit": limit,
            "respect_gitignore": respect_gitignore,
        },
        timeout_ms=timeout_ms,
    )
    matches = data.get("matches", []) if isinstance(data.get("matches"), list) else []
    status = str(data.get("status", "failed"))
    visible_matches, text_truncated = _compact_matches([str(item.get("path", item)) for item in matches])
    result = make_result(
        tool="remote.glob",
        target=endpoint.to_result_target(),
        outcome="success" if status == "ok" else "failed",
        status=status,
        summary=f"RemoteGlob found {len(matches)} paths.",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"matches": visible_matches, "truncated": bool(data.get("truncated", False)) or text_truncated},
        warnings=["respect_gitignore is not implemented for RemoteGlob"] if respect_gitignore else [],
        extra={"matches": visible_matches, "truncated": bool(data.get("truncated", False)) or text_truncated, "error": data.get("error")},
    )
    text = compact_text("\n".join(visible_matches) + ("\n<truncated>\n" if data.get("truncated") or text_truncated else "\n"))
    return {"text": text, "result": result}


def remote_grep(
    endpoint: Endpoint,
    *,
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    type: str | None = None,
    output_mode: str = "files_with_matches",
    multiline: bool = False,
    limit: int = 100,
    timeout_ms: int = 120000,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    local_warnings = []
    if limit > MAX_GREP_MATCHES:
        local_warnings.append(f"limit clamped from {limit} to {MAX_GREP_MATCHES}")
        limit = MAX_GREP_MATCHES
    if limit < 1:
        limit = 1
    raw_path = path or endpoint.effective_cwd
    try:
        base = join_under_root(endpoint.root, endpoint.effective_cwd, raw_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.grep", raw_path, str(exc), started, start)
    data = run_remote_python(
        endpoint,
        REMOTE_SEARCH_PY,
        {
            "op": "grep",
            "root": endpoint.root,
            "cwd": endpoint.effective_cwd,
            "path": base,
            "pattern": pattern,
            "glob": glob,
            "type": type,
            "output_mode": output_mode,
            "multiline": multiline,
            "limit": limit,
            "max_line_chars": MAX_LINE_CHARS,
        },
        timeout_ms=timeout_ms,
    )
    matches = data.get("matches", []) if isinstance(data.get("matches"), list) else []
    status = str(data.get("status", "failed"))
    warnings = local_warnings + (data.get("warnings", []) if isinstance(data.get("warnings"), list) else [])
    visible_matches, text_truncated = _compact_matches(matches)
    result = make_result(
        tool="remote.grep",
        target=endpoint.to_result_target(),
        outcome="success" if status == "ok" else "failed",
        status=status,
        summary=f"RemoteGrep found {len(matches)} matches.",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"matches": visible_matches, "truncated": bool(data.get("truncated", False)) or text_truncated},
        warnings=warnings,
        extra={"matches": visible_matches, "engine": data.get("engine"), "output_mode": output_mode, "truncated": bool(data.get("truncated", False)) or text_truncated, "error": data.get("error")},
    )
    text = compact_text("\n".join(visible_matches) + ("\n<truncated>\n" if data.get("truncated") or text_truncated else "\n"))
    return {"text": text, "result": result}


def _path_blocked_result(endpoint: Endpoint, tool: str, path: str, error: str, started: str, start: float) -> dict[str, Any]:
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
