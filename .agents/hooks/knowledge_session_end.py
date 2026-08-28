#!/usr/bin/env python3
"""Flush only explicitly deferred knowledge candidates for one Codex session."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_knowledge import (  # noqa: E402
    MAX_CANDIDATE_BYTES,
    KnowledgeError,
    capture_candidate,
    knowledge_session_key,
    load_candidate,
)

MAX_PENDING_PER_SESSION = 32


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _resolve_repo_root(payload: Mapping[str, Any]) -> Path:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        current = Path(cwd).resolve()
        for candidate in (current, *current.parents):
            if (candidate / ".agents" / "lib" / "vaws_knowledge.py").is_file():
                return candidate
    return ROOT


def process_session_end(
    payload: Mapping[str, Any], *, repo_root: Path | None = None
) -> dict[str, Any] | None:
    if payload.get("hook_event_name") != "SessionEnd":
        raise KnowledgeError("knowledge hook only accepts SessionEnd events")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise KnowledgeError("SessionEnd payload requires session_id")
    root = repo_root or _resolve_repo_root(payload)
    session_key = knowledge_session_key(session_id)
    pending_dir = (
        root / ".vaws-local" / "knowledge" / "pending" / session_key
    )
    if not pending_dir.is_dir():
        return None

    pending_paths = sorted(pending_dir.glob("*.json"))
    processed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    if len(pending_paths) > MAX_PENDING_PER_SESSION:
        errors.append(
            {
                "file": pending_dir.name,
                "error": (
                    f"pending candidate count {len(pending_paths)} exceeds "
                    f"{MAX_PENDING_PER_SESSION}"
                ),
            }
        )
        pending_paths = pending_paths[:MAX_PENDING_PER_SESSION]

    candidate_dir = root / ".vaws-local" / "knowledge" / "candidates"
    knowledge_dir = root / ".agents" / "knowledge"
    for path in pending_paths:
        try:
            if path.is_symlink():
                raise KnowledgeError("pending candidate must not be a symbolic link")
            if path.stat().st_size > MAX_CANDIDATE_BYTES:
                raise KnowledgeError(
                    f"pending candidate exceeds {MAX_CANDIDATE_BYTES} bytes"
                )
            candidate = load_candidate(path)
            if candidate["source"].get("session_id") != session_id:
                raise KnowledgeError("pending candidate session does not match hook session")
            result = capture_candidate(
                candidate,
                candidate_dir=candidate_dir,
                knowledge_dir=knowledge_dir,
            )
            if result["status"] not in {"passed", "already-promoted"}:
                raise KnowledgeError(f"unexpected capture status: {result['status']}")
            path.unlink()
            processed.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "capture_status": result["status"],
                    "action": result.get("action"),
                }
            )
        except (KnowledgeError, OSError) as exc:
            errors.append({"file": path.name, "error": str(exc)})

    try:
        pending_dir.rmdir()
    except OSError:
        pass
    receipt = {
        "schema_version": 1,
        "session_key": session_key,
        "status": "passed" if not errors else "partial",
        "processed": processed,
        "errors": errors,
        "processed_at": utc_now(),
    }
    receipt_path = (
        root
        / ".vaws-local"
        / "knowledge"
        / "session-end"
        / f"{session_key}.json"
    )
    _write_json_atomic(receipt_path, receipt)
    return receipt


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise KnowledgeError("hook input root must be an object")
        process_session_end(payload)
    except (KnowledgeError, OSError, json.JSONDecodeError):
        # SessionEnd is advisory. Never block shutdown or emit model-visible output.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
