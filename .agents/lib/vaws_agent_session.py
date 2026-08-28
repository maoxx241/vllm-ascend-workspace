"""Local development-task identity, independent of the remote fleet.

Native sessions are attachments, not task ids. A new native root session creates
a new task; resuming that same native session retains its task. Only an explicit
association or a child attachment joins another existing task. No operation here
allocates resources, reads transcripts, resets sources or removes worktrees.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from vaws_local_state import agent_sessions_root

CLIENTS = {"claude", "grok", "kimi", "codex", "cursor"}


def worktree_reference(path: str) -> dict:
    """Inspect an actual repository; never materialize a second source copy."""
    source = Path(path).expanduser().resolve(strict=True)
    result = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    root = Path(result.stdout.strip()).resolve()
    info = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir", "HEAD"],
        capture_output=True, text=True, timeout=5, check=True,
    ).stdout.splitlines()
    return {"path": str(root), "git_common_dir": str((root / info[0]).resolve()), "head_at_bind": info[1]}


class AgentSessions:
    def __init__(self, state_dir: Path | None = None):
        self.state_dir = (state_dir or agent_sessions_root()).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.state_dir / "sessions.sqlite3"
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id))")

    @contextlib.contextmanager
    def transaction(self):
        with sqlite3.connect(self.db_path, timeout=10) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def get(db, kind, key):
        row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if row is None:
            raise ValueError(f"unknown VAWS {kind}")
        return json.loads(row[0])

    @staticmethod
    def put(db, kind, value):
        db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)", (kind, value["id"], json.dumps(value, sort_keys=True)))

    @staticmethod
    def rows(db, kind):
        return [json.loads(row[0]) for row in db.execute("SELECT data FROM records WHERE kind=? ORDER BY rowid", (kind,))]

    def context(self, attachment_id: str) -> dict:
        with self.transaction() as db:
            attachment = self.get(db, "attachment", attachment_id)
            session = self.get(db, "session", attachment["session_id"])
        return {"schema_version": "vaws.agent-context.v1", "state_dir": str(self.state_dir),
                "session": session, "attachment": attachment,
                "context_file": str(self.state_dir / "contexts" / (attachment_id + ".json"))}

    def _publish(self, attachment_id):
        context = self.context(attachment_id)
        path = Path(context["context_file"])
        path.parent.mkdir(exist_ok=True, mode=0o700)
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        with temporary.open("x") as stream:
            os.chmod(temporary, 0o600)
            json.dump({key: context[key] for key in ("schema_version", "state_dir")}
                      | {"attachment_id": attachment_id}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return context

    def attach(self, client: str, native_session_id: str, cwd: str, *, parent_context: str = "",
               association: str = "", agent_id: str = "") -> dict:
        if client not in CLIENTS or not native_session_id or len(native_session_id) > 512:
            raise ValueError("a supported client and its actual native session id are required")
        if parent_context and association:
            raise ValueError("choose child inheritance or an explicit task association")
        parent = load_context(parent_context or association) if parent_context or association else None
        if parent and Path(parent["state_dir"]) != self.state_dir:
            raise ValueError("use the associated task's local registry; do not duplicate its identity")
        now = time.time()
        # Native identity, not cwd/window/PID/history recency, distinguishes a
        # new task from resume. Subagent ids supplement clients that reuse the
        # parent's native session id for child conversations.
        identity = [client, native_session_id, agent_id]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        with self.transaction() as db:
            existing = [row for row in self.rows(db, "attachment") if row["id"] == key]
            if not existing:
                session_id = parent["session"]["id"] if parent else "vaws-" + uuid.uuid4().hex
                if parent:
                    session = self.get(db, "session", session_id)
                    if session["state"] != "open":
                        raise ValueError("task is finished; explicitly reopen it before attaching")
                else:
                    self.put(db, "session", {"id": session_id, "state": "open", "created_at": now, "sources": {}})
                self.put(db, "attachment", {
                    "id": key, "session_id": session_id, "client": client,
                    "native_session_id": native_session_id, "agent_id": agent_id or None,
                    "parent_id": parent["attachment"]["id"] if parent_context else None,
                    "association": "child" if parent_context else "explicit" if association else "new-task",
                    "cwd": str(Path(cwd).expanduser().resolve()), "state": "attached", "created_at": now,
                })
            else:
                old = existing[0]
                if parent and old["session_id"] != parent["session"]["id"]:
                    raise ValueError("native session already belongs to another task")
                old.update(state="attached", resumed_at=now)
                self.put(db, "attachment", old)
                if not parent_context:
                    session = self.get(db, "session", old["session_id"])
                    if session["state"] == "finished":
                        session["state"] = "open"
                        self.put(db, "session", session)
        return self._publish(key)

    def native_context(self, client: str, native_session_id: str, agent_id: str = "") -> dict:
        with self.transaction() as db:
            matches = [row for row in self.rows(db, "attachment")
                       if row["client"] == client and row["native_session_id"] == native_session_id
                       and (row.get("agent_id") or "") == agent_id and row["state"] == "attached"]
        if len(matches) != 1:
            raise ValueError("native session association is missing or ambiguous; supply its explicit context")
        return self.context(matches[0]["id"])

    def bind_sources(self, context: dict, sources: dict[str, str]) -> dict:
        references = {}
        for name, path in sources.items():
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ValueError("source names must be single repository names")
            references[name] = worktree_reference(path)
        with self.transaction() as db:
            session = self.get(db, "session", context["session"]["id"])
            if session["state"] != "open":
                raise ValueError("resume the task before binding sources")
            if any(row["session_id"] == session["id"] and row["phase"] not in
                   {"succeeded", "failed", "timeout", "cancelled", "inconclusive"}
                   for row in self.rows(db, "execution")):
                if any(session["sources"].get(name, {}).get("path") != ref["path"] for name, ref in references.items()):
                    raise ValueError("finish pending executions before changing source worktree references")
            session["sources"].update(references)
            self.put(db, "session", session)
        return self.context(context["attachment"]["id"])

    def detach(self, context: dict) -> dict:
        with self.transaction() as db:
            attachment = self.get(db, "attachment", context["attachment"]["id"])
            attachment.update(state="detached", detached_at=time.time())
            self.put(db, "attachment", attachment)
        # Detaching a frontend never stops a job or releases a lease.
        return self.context(attachment["id"])

    def executions(self, session_id: str) -> list[dict]:
        with self.transaction() as db:
            return [row for row in self.rows(db, "execution") if row["session_id"] == session_id]

    def execution(self, context: dict, request_id: str, spec: dict) -> dict:
        key = hashlib.sha256(json.dumps([context["session"]["id"], request_id]).encode()).hexdigest()
        with self.transaction() as db:
            matches = [row for row in self.rows(db, "execution") if row["id"] == key]
            if matches:
                if matches[0]["spec"] != spec:
                    raise ValueError("execution request id reused with different arguments")
                return matches[0]
            if self.get(db, "session", context["session"]["id"])["state"] != "open":
                raise ValueError("resume the task before starting another execution")
            row = {"id": key, "session_id": context["session"]["id"], "request_id": request_id,
                   "spec": spec, "phase": "planned", "created_at": time.time()}
            self.put(db, "execution", row)
            return row

    def save_execution(self, row):
        with self.transaction() as db:
            self.put(db, "execution", row)


def load_context(context_file: str = "") -> dict:
    filename = context_file or os.environ.get("VAWS_CONTEXT_FILE", "")
    if not filename:
        raise ValueError("VAWS context is required; use the native session hook or explicit task association")
    path = Path(filename).expanduser().resolve(strict=True)
    reference = json.loads(path.read_text())
    if reference.get("schema_version") != "vaws.agent-context.v1":
        raise ValueError("not a VAWS agent context")
    store = AgentSessions(Path(reference["state_dir"]))
    context = store.context(reference["attachment_id"])
    if Path(context["context_file"]) != path:
        raise ValueError("context path does not match its registered attachment")
    return context
