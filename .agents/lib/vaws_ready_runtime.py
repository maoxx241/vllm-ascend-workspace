"""Shared ready-runtime registry. Host NpuCoordinator remains card authority.

One manager process owns this database. Transactions persist intent before
remote calls, whose idempotent task ids survive manager restart. Managed jobs
may stop only their recorded process families. This never creates containers,
installs packages or stops an unrelated service.
"""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from vaws_runtime_profile import digest
from vaws_run_manifest import new_manifest, write_manifest, utc_now
from vaws_managed_execution import JOB_TERMINAL, ManagedExecution

TERMINAL = {"released", "cancelled", "expired"}


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", value):
        raise ValueError("invalid identifier")
    return value


def endpoint(value: dict[str, Any], *, container: bool = False) -> dict[str, Any]:
    host, user, port = value.get("host", ""), value.get("user", "root"), int(value.get("port", 22))
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]*", host) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", user) or not 0 < port < 65536:
        raise ValueError("invalid endpoint")
    result = {"host": host, "port": port, "user": user}
    if container:
        root = value.get("root", "")
        if not root.startswith("/") or root == "/" or ".." in PurePosixPath(root).parts:
            raise ValueError("a dedicated absolute runtime root is required")
        result.update(root=str(PurePosixPath(root)), cwd=str(PurePosixPath(root)))
    return result


class RuntimePool(ManagedExecution):
    def __init__(self, state_dir: Path, backend: Any, *, clock=time.time):
        self.state_dir, self.backend, self.clock = state_dir, backend, clock
        state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = state_dir / "coordinator.sqlite3"
        self.lock = threading.RLock()
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id))")
            db.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, data TEXT NOT NULL)")

    @contextlib.contextmanager
    def transaction(self):
        with sqlite3.connect(self.db_path, timeout=60) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def get(db, kind, key):
        row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if row is None:
            raise ValueError(f"unknown {kind}")
        return json.loads(row[0])

    @staticmethod
    def put(db, kind, row):
        db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)", (kind, row["id"], json.dumps(row, sort_keys=True)))

    @staticmethod
    def rows(db, kind):
        return [json.loads(row[0]) for row in db.execute("SELECT data FROM records WHERE kind=? ORDER BY rowid", (kind,))]

    def owned(self, db, kind, key, owner):
        row = self.get(db, kind, key)
        if row["owner"] != owner:
            raise PermissionError("resource belongs to another principal")
        return row

    def event(self, db, owner, kind, **data):
        event = {"kind": kind, "at": self.clock(), **data}
        cur = db.execute("INSERT INTO events(owner,data) VALUES(?,?)", (owner, json.dumps(event)))
        return {"cursor": cur.lastrowid, **event}

    def session_open(self, owner: str, session_id: str, sources: dict[str, str]):
        safe_id(owner)
        safe_id(session_id)
        if not sources or any(not isinstance(path, str) or not Path(path).is_absolute() for path in sources.values()):
            raise ValueError("record the actual absolute local business worktree paths")
        key = digest([owner, session_id])
        with self.lock, self.transaction() as db:
            for old in self.rows(db, "session"):
                if old["id"] == key:
                    if old["sources"] != sources:
                        bindings = [row for row in self.rows(db, "binding")
                                    if row["intent"]["session"] == key and row["state"] != "returned"]
                        if bindings:
                            raise ValueError("return task runtimes before changing source worktree references")
                        old["sources"] = sources
                        self.put(db, "session", old)
                    return old
            row = {"id": key, "owner": owner, "session_id": session_id, "sources": sources}
            self.put(db, "session", row)
            return row

    def register(self, runtime_id: str, spec: dict[str, Any]):
        """Administrator adopts a prepared idle container; never provisions it."""
        safe_id(runtime_id)
        spec = {"endpoint": endpoint(spec["endpoint"], container=True),
                "host_endpoint": endpoint(spec["host_endpoint"]),
                "container_name": safe_id(spec["container_name"]), "service_ports": spec.get("service_ports", [])}
        ports = spec["service_ports"]
        if not isinstance(ports, list) or any(type(port) is not int or not 0 < port < 65536 for port in ports) or len(set(ports)) != len(ports):
            raise ValueError("service_ports must contain distinct TCP ports")
        if spec["endpoint"]["port"] in ports or spec["host_endpoint"]["port"] in ports:
            raise ValueError("service ports cannot overlap SSH endpoints")
        with self.lock:
            with self.transaction() as db:
                for other in self.rows(db, "runtime"):
                    if other["id"] == runtime_id:
                        if other["state"] == "bound":
                            raise ValueError("runtime is still bound; return it first")
                    elif (other["endpoint"]["host"], other["endpoint"]["port"]) == (spec["endpoint"]["host"], spec["endpoint"]["port"]) or (other["host_endpoint"], other["container_name"]) == (spec["host_endpoint"], spec["container_name"]):
                        raise ValueError("container/endpoint already registered")
                    elif other["host_endpoint"] == spec["host_endpoint"] and (set(other.get("service_ports", [])) | {other["endpoint"]["port"]}) & (set(ports) | {spec["endpoint"]["port"]}):
                        raise ValueError("runtime ports overlap another registered runtime")
            observed = self.backend.inspect(spec, idle=True)
            row = {"id": runtime_id, **spec, "state": "ready", "attestation": observed}
            with self.transaction() as db:
                self.put(db, "runtime", row)
            return row

    def checkout(self, owner: str, session: str, profile_key: str, request_id: str, runtime_id: str = ""):
        safe_id(request_id)
        key = digest([owner, request_id])
        intent = {"session": session, "profile_key": profile_key, "runtime_id": runtime_id}
        with self.lock:
            with self.transaction() as db:
                self.owned(db, "session", session, owner)
                for binding in self.rows(db, "binding"):
                    if binding["id"] == key:
                        if binding["intent"] != intent:
                            raise ValueError("request id reused with different checkout parameters")
                        return binding
                candidates = [row for row in self.rows(db, "runtime") if row["state"] == "ready"
                              and row["attestation"]["profile_key"] == profile_key
                              and (not runtime_id or row["id"] == runtime_id)]
            for runtime in candidates:
                try:
                    observed = self.backend.inspect(runtime, idle=True)
                    if observed != runtime["attestation"]:
                        raise ValueError("prepared environment changed; re-register it")
                except Exception as exc:
                    runtime["state"] = "needs_repair"
                    runtime["error"] = str(exc)[:500]
                    with self.transaction() as db:
                        self.put(db, "runtime", runtime)
                    continue
                row = {"id": key, "owner": owner, "intent": intent, "runtime_id": runtime["id"],
                       "state": "bound", "endpoint": runtime["endpoint"],
                       "profile_key": profile_key, "build_key": observed["build_key"],
                       "service_ports": runtime["service_ports"],
                       "environment": {"VAWS_ENVIRONMENT_FINGERPRINT": profile_key},
                       "build_env": observed["profile"].get("build_env", {}),
                       "launch_env": observed["profile"]["launch_env"],
                       "launch_preamble": observed.get("launch_preamble", "")}
                runtime["state"] = "bound"
                with self.transaction() as db:
                    self.put(db, "runtime", runtime)
                    self.put(db, "binding", row)
                    self.event(db, owner, "runtime-bound", binding=key, runtime=runtime["id"])
                return row
            return {"status": "cache_miss", "reason": "no verified ready runtime matches the requested profile",
                    "provisioning_started": False}

    def drain(self, runtime_id):
        """Stop new checkouts, without stopping an owner's in-flight work."""
        with self.lock, self.transaction() as db:
            runtime = self.get(db, "runtime", runtime_id)
            runtime["draining"] = True
            if runtime["state"] != "bound":
                runtime["state"] = "draining"
            self.put(db, "runtime", runtime)
            return {"runtime_id": runtime_id, "state": runtime["state"], "draining": True,
                    "next": "Wait for existing bindings to return before maintenance; re-register after verification."}

    def return_runtime(self, owner: str, binding_id: str):
        with self.lock, self.transaction() as db:
            binding = self.owned(db, "binding", binding_id, owner)
            if any(run["binding_id"] == binding_id and run["state"] not in TERMINAL for run in self.rows(db, "run")):
                raise ValueError("resolve/release execution leases before returning the runtime")
            runtime = self.get(db, "runtime", binding["runtime_id"])
            if binding["state"] != "returned":
                binding["state"] = "returned"
                runtime["state"] = "draining" if runtime.get("draining") else "needs_repair"
                self.put(db, "binding", binding)
                self.put(db, "runtime", runtime)
                self.event(db, owner, "runtime-returned", binding=binding_id)
            return {"status": "returned", "runtime_state": runtime["state"],
                    "next": "owner cleans only its workers; administrator re-verifies and registers the idle runtime"}

    def refresh(self, owner: str, binding_id: str):
        """Accept an owner's explicitly prepared new native bundle, before queueing."""
        with self.lock:
            with self.transaction() as db:
                binding = self.owned(db, "binding", binding_id, owner)
                if binding["state"] != "bound" or any(run["binding_id"] == binding_id and run["state"] not in TERMINAL for run in self.rows(db, "run")):
                    raise ValueError("finish/release executions before refreshing prepared artifacts")
                runtime = self.get(db, "runtime", binding["runtime_id"])
            observed = self.backend.inspect(runtime, idle=True)
            if observed["profile_key"] != binding["profile_key"]:
                raise ValueError("environment changed; return it and request the new profile")
            runtime["attestation"] = observed
            binding["build_key"] = observed["build_key"]
            binding["launch_preamble"] = observed.get("launch_preamble", "")
            with self.transaction() as db:
                self.put(db, "runtime", runtime)
                self.put(db, "binding", binding)
                self.event(db, owner, "runtime-refreshed", binding=binding_id, build_key=binding["build_key"])
            return binding

    def request_run(self, owner: str, binding_id: str, request_id: str, snapshots: dict[str, str],
                    expected_build_key: str, devices: list[int], npu_count: int,
                    priority: int = 0, queue_seconds: int = 1800):
        safe_id(request_id)
        if bool(devices) == bool(npu_count) or any(type(d) is not int or d < 0 for d in devices) or len(set(devices)) != len(devices) or npu_count < 0:
            raise ValueError("supply distinct physical devices OR a positive npu_count")
        if not 1 <= queue_seconds <= 86400:
            raise ValueError("queue_seconds must be between 1 and 86400")
        if not {"vllm", "vllm-ascend"}.issubset(snapshots) or any(not re.fullmatch(r"[0-9a-f]{40,64}", commit) for commit in snapshots.values()):
            raise ValueError("pin the complete parity snapshot map before requesting cards")
        for name in snapshots:
            if name != "." and (PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts):
                raise ValueError("unsafe snapshot path")
        key = digest([owner, binding_id, request_id])
        intent = {"snapshots": snapshots, "build_key": expected_build_key, "devices": devices,
                  "npu_count": npu_count, "priority": priority, "queue_seconds": queue_seconds}
        with self.lock:
            with self.transaction() as db:
                binding = self.owned(db, "binding", binding_id, owner)
                runtime = self.get(db, "runtime", binding["runtime_id"])
                for run in self.rows(db, "run"):
                    if run["id"] == key:
                        if run["intent"] != intent:
                            raise ValueError("run request id reused with different parameters")
                        return run
                    if run["binding_id"] == binding_id and run["state"] not in TERMINAL:
                        raise ValueError("binding already has an unresolved execution")
                if binding["state"] != "bound" or expected_build_key != binding["build_key"]:
                    raise ValueError("cache miss or returned binding; prepare matching artifacts first")
            observed = self.backend.inspect(runtime, idle=True, snapshots=snapshots)
            if observed != runtime["attestation"]:
                raise ValueError("runtime/profile changed since checkout")
            run = {"id": key, "owner": owner, "binding_id": binding_id, "intent": intent,
                   "task_id": "pool-" + uuid.uuid4().hex, "state": "pending", "epoch": None,
                   "deadline": self.clock() + queue_seconds, "last_poll": 0, "created_at": utc_now(), "submitted": False}
            with self.transaction() as db:
                self.put(db, "run", run)  # durable intent BEFORE the first host request
                self.event(db, owner, "run-queued", run=key)
            self.export_manifest(run, binding)
            return self.control(owner, key, "poll")

    def export_manifest(self, run, binding, job=None):
        if job is None:
            with self.transaction() as db:
                job = next((row for row in self.rows(db, "job") if row["id"] == run["id"]), None)
        manifest = new_manifest(run_type="debug", run_id="pool-" + run["id"], created_at=run["created_at"],
                                workspace_snapshot=run["intent"]["snapshots"],
                                environment={"profile_key": binding["profile_key"], "build_key": binding["build_key"],
                                             "endpoint": binding["endpoint"]},
                                topology={"physical_devices": run.get("task", {}).get("granted_devices", [])})
        # A released allocation says nothing about model correctness/readiness.
        manifest["status"] = ("inconclusive" if run["state"] in TERMINAL and run.get("task", {}).get("started_at")
                              else "cancelled" if run["state"] in TERMINAL else "running" if run.get("task", {}).get("started_at") else "planned")
        manifest["updated_at"] = utc_now()
        manifest["environment"]["coordination"] = {"state": run["state"], "task_id": run["task_id"], "epoch": run["epoch"]}
        if job:
            manifest["environment"]["managed_execution"] = {
                "job_id": job["job_id"], "state": job["state"], "runtime_id": binding["runtime_id"],
                "remote_dir": job.get("remote", {}).get("remote_dir"),
                "result": job.get("remote", {}).get("result"),
            }
            # Shell exit 0 alone does not establish a model-level pass.
            if job["state"] in {"failed", "timeout"}:
                manifest["status"] = "failed"
            elif job["state"] == "cancelled":
                manifest["status"] = "cancelled"
            elif job["state"] in {"succeeded", "inconclusive"}:
                manifest["status"] = "inconclusive"
        write_manifest(self.state_dir / "runs" / (run["id"] + ".json"), manifest)

    def control(self, owner: str, run_id: str, action: str, pid: int = 0, *, _managed=False,
                process_guard=None, completion_confirmed=False):
        if action not in {"poll", "preflight", "activate", "heartbeat", "release", "cancel"}:
            raise ValueError("unsupported execution action")
        if completion_confirmed and not _managed:
            raise ValueError("only managed supervision can confirm descendant completion")
        with self.lock:
            with self.transaction() as db:
                run = self.owned(db, "run", run_id, owner)
                binding = self.owned(db, "binding", run["binding_id"], owner)
                runtime = self.get(db, "runtime", binding["runtime_id"])
                if not _managed and action != "poll" and any(job["id"] == run_id for job in self.rows(db, "job")):
                    raise ValueError("managed execution owns this lease; use managed_execution_control")
            if run["state"] in TERMINAL:
                return run
            try:
                if run["state"] == "uncertain":
                    # A previous timed-out action may have succeeded. Recover by
                    # observing its exact task; never submit a replacement.
                    snapshot = self.backend.host(runtime, {"action": "status", "no_probe": False})
                    if snapshot["coordination_epoch"] != run["epoch"]:
                        raise ValueError("host epoch changed; ownership requires operator reconciliation")
                    matches = [task for task in snapshot["tasks"] if task["task_id"] == run["task_id"]]
                    if not matches and run["submitted"]:
                        raise ValueError("host task disappeared; ownership remains unresolved")
                    if matches:
                        run["task"] = matches[0]
                        run["state"] = matches[0]["state"]
                        run["submitted"] = True
                    else:
                        run["state"] = "pending"
                    # Observation is not a retry of a possibly non-idempotent
                    # activate/preflight action. Caller observes before retry.
                    action = "poll"
                if run["epoch"] is None:
                    status = self.backend.host(runtime, {"action": "status", "no_probe": True})
                    run["epoch"] = status["coordination_epoch"]
                    with self.transaction() as db:
                        self.put(db, "run", run)
                request = {"task_id": run["task_id"], "coordination_epoch": run["epoch"]}
                if run["state"] == "pending":
                    status = self.backend.host(runtime, {"action": "status", "no_probe": True, "coordination_epoch": run["epoch"]})
                    existing = [task for task in status["tasks"] if task["task_id"] == run["task_id"]]
                    if existing:
                        run["task"], run["state"], run["submitted"] = existing[0], existing[0]["state"], True
                    elif self.clock() >= run["deadline"] or action == "cancel":
                        run["state"] = "expired" if action != "cancel" else "cancelled"
                if run["state"] == "pending":
                    intent = run["intent"]
                    submit = {**request, "action": "submit", "agent_id": "mcp-" + digest(owner)[:32],
                              "session_id": binding["intent"]["session"], "container_name": runtime["container_name"],
                              "priority": intent["priority"], "latest_start": run["deadline"],
                              "estimated_duration_seconds": 1800}
                    submit.update({"devices": intent["devices"]} if intent["devices"] else {"npu_count": intent["npu_count"]})
                    reply = self.backend.host(runtime, submit)
                    run["task"], run["state"] = reply["task"], reply["task"]["state"]
                    run["submitted"] = True
                    with self.transaction() as db:
                        self.put(db, "run", run)
                if run["state"] in TERMINAL:
                    reply = {"task": run.get("task", {"state": run["state"]})}
                elif action == "poll":
                    if run["state"] == "queued":
                        reply = self.backend.host(runtime, {**request, "action": "acquire"})
                    else:
                        status = self.backend.host(runtime, {**request, "action": "status", "no_probe": False})
                        reply = {"task": status["tasks"][0]}
                else:
                    if action == "preflight":
                        observed = self.backend.inspect(runtime, idle=True, snapshots=run["intent"]["snapshots"])
                        if observed != runtime["attestation"]:
                            raise ValueError("runtime changed before launch")
                    reply = self.backend.host(runtime, {**request, "action": action,
                              "fence_token": run.get("task", {}).get("fence_token"), "pid": pid,
                              **({"completion_confirmed": True} if action == "release" and completion_confirmed else {}),
                              **({"process_guard": process_guard} if action == "activate" and process_guard else {})})
                if reply.get("task"):
                    run["task"], run["state"] = reply["task"], reply["task"]["state"]
                    run.pop("error", None)
                else:
                    raise ValueError(reply.get("error", "host probe did not return a task"))
                run["environment"] = {"ASCEND_RT_VISIBLE_DEVICES": ",".join(map(str, run["task"].get("granted_devices", [])))}
            except Exception as exc:
                # No host epoch means no mutating host request was sent yet.
                run["state"] = "uncertain" if run["epoch"] is not None else "pending"
                run["error"] = str(exc)[:500]
            run["last_poll"] = self.clock()
            with self.transaction() as db:
                previous = self.get(db, "run", run_id)
                self.put(db, "run", run)
                if (previous["state"], previous.get("error")) != (run["state"], run.get("error")):
                    self.event(db, owner, "run-state", run=run_id, state=run["state"], error=run.get("error"))
            self.export_manifest(run, binding)
            return run

    def reconcile(self, owner: str, run_id: str, reason: str):
        """Force a wedged uncertain run terminal after operator inspection.

        Host coordination state lives in /tmp and a reboot starts a new epoch,
        which otherwise wedges every uncertain run of the pool forever. This
        never probes or guesses ownership: the administrator asserts it after
        inspecting the host, and the assertion is recorded as a durable event.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("record the host inspection evidence as a bounded reason")
        with self.lock, self.transaction() as db:
            run = self.get(db, "run", run_id)
            if run["state"] != "uncertain":
                raise ValueError("only an uncertain run wedged by lost host state can be reconciled")
            run["state"] = "cancelled"
            run["error"] = "operator reconcile: " + reason.strip()[:500]
            self.put(db, "run", run)
            jobs = [row for row in self.rows(db, "job")
                    if row["id"] == run_id and row["state"] not in JOB_TERMINAL]
            for job in jobs:
                job.update(state="inconclusive", error="operator reconcile: " + reason.strip()[:500])
                self.put(db, "job", job)
            binding = self.get(db, "binding", run["binding_id"])
            event = self.event(db, owner, "run-reconciled", run=run_id,
                               reason=reason.strip()[:500], previous="uncertain")
        self.export_manifest(run, binding, job=jobs[0] if jobs else None)
        return {"run": run, "jobs": jobs, "event": event,
                "next": "return the runtime for quarantine and re-verification before any reuse"}

    def tick(self, limit: int = 4):
        """Observe manual leases and supervise explicitly registered jobs."""
        with self.transaction() as db:
            managed = {row["id"] for row in self.rows(db, "job")}
            rows = [row for row in self.rows(db, "run") if row["state"] not in TERMINAL and row["id"] not in managed]
        for row in sorted(rows, key=lambda row: row["last_poll"])[:limit]:
            self.control(row["owner"], row["id"], "poll")
        self.managed_tick(limit)

    def status(self, owner: str):
        with self.transaction() as db:
            return {kind + "s": [row for row in self.rows(db, kind) if row["owner"] == owner]
                    for kind in ("session", "binding", "run", "job")}

    def peers(self):
        with self.transaction() as db:
            return [{"run": row["id"], "owner": row["owner"], "state": row["state"],
                     "devices": row.get("task", {}).get("granted_devices", [])}
                    for row in self.rows(db, "run") if row["state"] not in TERMINAL]

    def catalog(self):
        with self.transaction() as db:
            return [{"runtime_id": row["id"], "state": row["state"],
                     "draining": row.get("draining", False),
                     "profile_key": row["attestation"]["profile_key"],
                     "build_key": row["attestation"]["build_key"], "service_ports": row.get("service_ports", []),
                     "error": row.get("error")}
                    for row in self.rows(db, "runtime")]

    def message(self, owner: str, target_run: str, text: str):
        if not text.strip() or len(text) > 4000:
            raise ValueError("message must contain 1..4000 characters")
        with self.transaction() as db:
            run = self.get(db, "run", target_run)
            return self.event(db, run["owner"], "coordination-message", sender=owner, run=target_run, text=text)

    def reply(self, owner: str, cursor: int, text: str):
        if not text.strip() or len(text) > 4000:
            raise ValueError("reply must contain 1..4000 characters")
        with self.transaction() as db:
            row = db.execute("SELECT owner,data FROM events WHERE id=?", (cursor,)).fetchone()
            if row is None or row[0] != owner:
                raise PermissionError("message belongs to another principal")
            message = json.loads(row[1])
            if "sender" not in message:
                raise ValueError("event is not a message")
            return self.event(db, message["sender"], "coordination-reply", sender=owner, reply_to=cursor, text=text)

    def events(self, owner: str, after: int = 0, limit: int = 100):
        with self.transaction() as db:
            rows = db.execute("SELECT id,data FROM events WHERE owner=? AND id>? ORDER BY id LIMIT ?",
                              (owner, after, min(100, max(1, limit)))).fetchall()
            return {"events": [{"cursor": row[0], **json.loads(row[1])} for row in rows],
                    "cursor": rows[-1][0] if rows else after}
