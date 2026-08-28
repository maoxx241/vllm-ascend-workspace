#!/usr/bin/env python3
"""Cooperative host-local NPU task coordination.

The coordinator is deliberately advisory.  It gives independent VAWS agents a
shared queue and lease ledger on one host, but it does not prevent an operator
or a non-participating process from using an NPU.  Observed hardware occupancy
always wins over declarations.

The module is stdlib-only because the agent-facing wrapper sends this source to
the bare-metal host and executes it there.  State defaults to
``/tmp/vaws-npu-coordinator/v1`` and is expected to disappear after a host or
``/tmp`` reset; a missing database simply starts a new coordination epoch.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

SCHEMA_VERSION = 2
DEFAULT_STATE_DIR = "/tmp/vaws-npu-coordinator/v1"
DEFAULT_QUEUE_TTL_SECONDS = 3600
DEFAULT_GRANT_TTL_SECONDS = 60
DEFAULT_START_TTL_SECONDS = 60
DEFAULT_HEARTBEAT_TTL_SECONDS = 120
DEFAULT_ESTIMATED_DURATION_SECONDS = 3600
HBM_BUSY_THRESHOLD_MB = 4096

TASK_STATES = {
    "queued",
    "granted",
    "starting",
    "active",
    "orphaned_busy",
    "released",
    "expired",
    "cancelled",
}
RESERVING_TASK_STATES = {"granted", "starting", "active", "orphaned_busy"}
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")


class CoordinationError(RuntimeError):
    """Raised for deterministic coordinator input or state failures."""


def utc_now_iso(epoch: float | None = None) -> str:
    value = time.time() if epoch is None else epoch
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_instant(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CoordinationError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise CoordinationError(f"timestamp must include a timezone: {value!r}")
    return parsed.timestamp()


def require_safe_id(value: str | None, *, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise CoordinationError(
            f"invalid {label}: use 3-128 characters from A-Z a-z 0-9 _ . : -"
        )
    return value


def parse_devices(value: Any, *, allow_none: bool = True) -> list[int] | None:
    if value is None:
        if allow_none:
            return None
        raise CoordinationError("devices are required")
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raise CoordinationError("devices must be a comma-separated string or list")
    devices: list[int] = []
    seen: set[int] = set()
    for raw in raw_items:
        token = str(raw).strip()
        if not token:
            raise CoordinationError("devices contains an empty device id")
        try:
            device = int(token, 10)
        except ValueError as exc:
            raise CoordinationError(f"devices contains a non-integer id: {token!r}") from exc
        if device < 0:
            raise CoordinationError(f"devices contains a negative id: {device}")
        if device in seen:
            raise CoordinationError(f"devices contains a duplicate id: {device}")
        seen.add(device)
        devices.append(device)
    if not devices:
        raise CoordinationError("devices must not be empty")
    return sorted(devices)


def _json_devices(value: list[int] | None) -> str | None:
    return None if value is None else json.dumps(sorted(value), separators=(",", ":"))


def _load_devices(value: str | None) -> list[int]:
    if not value:
        return []
    loaded = json.loads(value)
    return [int(item) for item in loaded]


def parse_npu_smi_info(output: str) -> dict[str, Any]:
    """Parse visible devices plus process/HBM occupancy from common layouts."""
    dev_ids: set[int] = set()
    header_ids: set[int] = set()
    hbm: dict[int, dict[str, int]] = {}
    current_npu: int | None = None
    lines = output.splitlines()

    for line in lines:
        if "0000:" in line:
            chip = re.match(r"\|\s*(\d+)\s+(\d+)\s+\|.*0000:", line)
            if chip:
                dev_id = int(chip.group(2))
            elif current_npu is not None:
                dev_id = current_npu
            else:
                continue
            dev_ids.add(dev_id)
            pairs = re.findall(r"(\d+)\s*/\s*(\d+)", line)
            if len(pairs) >= 2:
                hbm[dev_id] = {
                    "used_mb": int(pairs[-1][0]),
                    "total_mb": int(pairs[-1][1]),
                }
            continue
        header = re.match(r"\|\s*(\d+)\s+\d*\w+\d+\w*\s+\|", line)
        if header:
            current_npu = int(header.group(1))
            header_ids.add(current_npu)

    if not dev_ids:
        dev_ids.update(header_ids)

    process_busy: dict[int, list[dict[str, Any]]] = {}
    in_process_table = False
    for line in lines:
        if "Process name" in line or "Process memory" in line:
            in_process_table = True
            continue
        if in_process_table and "No running processes" in line:
            continue
        if in_process_table and line.startswith("|"):
            match = re.match(r"\|\s*(\d+)\s+\S+\s+(\d+)\s+(\S+)\s+(\S+)", line)
            if match:
                device = int(match.group(1))
                process_busy.setdefault(device, []).append(
                    {
                        "kind": "process",
                        "pid": int(match.group(2)),
                        "owner": match.group(3),
                        "name": match.group(4),
                    }
                )

    busy: dict[int, list[dict[str, Any]]] = {}
    for device in sorted(dev_ids):
        reasons = list(process_busy.get(device, []))
        usage = hbm.get(device)
        if usage and usage["used_mb"] >= HBM_BUSY_THRESHOLD_MB and not reasons:
            reasons.append(
                {
                    "kind": "hbm_threshold",
                    "hbm_used_mb": usage["used_mb"],
                    "threshold_mb": HBM_BUSY_THRESHOLD_MB,
                }
            )
        if reasons:
            busy[device] = reasons
    return {
        "status": "ok",
        "collected_at": utc_now_iso(),
        "devices": sorted(dev_ids),
        "busy": {str(key): value for key, value in sorted(busy.items())},
        "free": sorted(device for device in dev_ids if device not in busy),
        "hbm": {str(key): value for key, value in sorted(hbm.items())},
        "hbm_busy_threshold_mb": HBM_BUSY_THRESHOLD_MB,
    }


def probe_npu_occupancy() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["npu-smi", "info"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "error": str(exc), "devices": [], "busy": {}, "free": []}
    if result.returncode != 0:
        return {
            "status": "failed",
            "error": "npu-smi info failed",
            "returncode": result.returncode,
            "stderr": (result.stderr or result.stdout)[-2000:],
            "devices": [],
            "busy": {},
            "free": [],
        }
    parsed = parse_npu_smi_info(result.stdout)
    if not parsed["devices"]:
        return {
            "status": "failed",
            "error": "npu-smi output did not contain any parseable devices",
            "devices": [],
            "busy": {},
            "free": [],
        }
    return parsed


class NpuCoordinator:
    """SQLite-backed cooperative queue for one bare-metal host."""

    def __init__(
        self,
        state_dir: str | Path = DEFAULT_STATE_DIR,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / "coordinator.sqlite3"
        self.clock = clock
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    agent_alias TEXT,
                    session_id TEXT,
                    container_name TEXT,
                    requested_count INTEGER NOT NULL,
                    requested_devices TEXT,
                    not_before REAL NOT NULL,
                    latest_start REAL NOT NULL,
                    estimated_duration_seconds INTEGER NOT NULL,
                    preemptible INTEGER NOT NULL DEFAULT 0,
                    priority INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    submitted_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    granted_devices TEXT,
                    activation_deadline REAL,
                    fence_token INTEGER,
                    started_at REAL,
                    expected_end REAL,
                    heartbeat_at REAL,
                    heartbeat_deadline REAL,
                    pid INTEGER,
                    message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_queue
                    ON tasks(state, priority DESC, submitted_at ASC);
                CREATE TABLE IF NOT EXISTS holds (
                    hold_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    devices TEXT NOT NULL,
                    not_before REAL NOT NULL,
                    end_at REAL NOT NULL,
                    reason TEXT,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_holds_window
                    ON holds(state, not_before, end_at);
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    task_id TEXT,
                    data TEXT NOT NULL
                );
                """
            )
            task_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
            }
            if "agent_alias" not in task_columns:
                try:
                    connection.execute("ALTER TABLE tasks ADD COLUMN agent_alias TEXT")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('coordination_epoch', ?)",
                (str(uuid.uuid4()),),
            )
            connection.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('next_fence', '0')")
        finally:
            connection.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _event(
        self,
        connection: sqlite3.Connection,
        kind: str,
        *,
        task_id: str | None = None,
        data: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events(at, kind, task_id, data) VALUES(?, ?, ?, ?)",
            (self.clock() if now is None else now, kind, task_id, json.dumps(data or {}, sort_keys=True)),
        )

    def _next_fence(self, connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT value FROM meta WHERE key='next_fence'").fetchone()
        value = int(row["value"]) + 1
        connection.execute("UPDATE meta SET value=? WHERE key='next_fence'", (str(value),))
        return value

    @staticmethod
    def _busy_set(observed: dict[str, Any] | None) -> set[int] | None:
        if observed is None or observed.get("status") != "ok":
            return None
        return {int(key) for key in observed.get("busy", {})}

    @staticmethod
    def _serialize_task(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        payload["requested_devices"] = _load_devices(payload.get("requested_devices")) or None
        payload["granted_devices"] = _load_devices(payload.get("granted_devices"))
        payload["preemptible"] = bool(payload.get("preemptible"))
        for key in (
            "not_before",
            "latest_start",
            "submitted_at",
            "updated_at",
            "activation_deadline",
            "started_at",
            "expected_end",
            "heartbeat_at",
            "heartbeat_deadline",
        ):
            if payload.get(key) is not None:
                payload[key] = utc_now_iso(float(payload[key]))
        return payload

    @staticmethod
    def _serialize_hold(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        payload["devices"] = _load_devices(payload.get("devices"))
        for key in ("not_before", "end_at", "created_at", "updated_at"):
            payload[key] = utc_now_iso(float(payload[key]))
        return payload

    def _task_row(self, connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise CoordinationError(f"task not found: {task_id}")
        return row

    def _check_token(self, row: sqlite3.Row, token: int) -> None:
        if row["fence_token"] is None or int(row["fence_token"]) != int(token):
            raise CoordinationError(f"stale or invalid fencing token for task {row['task_id']}")

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        task_id = require_safe_id(request.get("task_id") or str(uuid.uuid4()), label="task id")
        agent_id = require_safe_id(request.get("agent_id"), label="agent id")
        agent_alias = request.get("agent_alias")
        if agent_alias is not None:
            agent_alias = require_safe_id(agent_alias, label="agent alias")
        session_id = request.get("session_id")
        if session_id is not None:
            session_id = require_safe_id(session_id, label="session id")
        devices = parse_devices(request.get("devices"))
        count_raw = request.get("npu_count")
        if devices is not None and count_raw is not None:
            raise CoordinationError("use only one of devices or npu_count")
        if devices is None:
            count = int(count_raw or 0)
            if count < 1:
                raise CoordinationError("npu_count must be >= 1 when devices are not specified")
        else:
            count = len(devices)
        duration = int(request.get("estimated_duration_seconds") or DEFAULT_ESTIMATED_DURATION_SECONDS)
        if duration < 1:
            raise CoordinationError("estimated_duration_seconds must be >= 1")
        not_before = parse_instant(request.get("not_before"))
        if not_before is None:
            not_before = now
        latest_start = parse_instant(request.get("latest_start"))
        if latest_start is None:
            queue_ttl = int(request.get("queue_ttl_seconds") or DEFAULT_QUEUE_TTL_SECONDS)
            if queue_ttl < 1:
                raise CoordinationError("queue_ttl_seconds must be >= 1")
            latest_start = now + queue_ttl
        if latest_start < not_before:
            raise CoordinationError("latest_start must not be earlier than not_before")
        priority = int(request.get("priority") or 0)
        container_name = request.get("container_name")
        with self._transaction() as connection:
            existing = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if existing is not None:
                expected = {
                    "agent_id": agent_id,
                    "agent_alias": agent_alias,
                    "session_id": session_id,
                    "container_name": container_name,
                    "requested_count": count,
                    "requested_devices": _json_devices(devices),
                }
                mismatched = {
                    key: {"existing": existing[key], "requested": value}
                    for key, value in expected.items()
                    if existing[key] != value
                }
                if mismatched:
                    raise CoordinationError(
                        f"task id {task_id} already exists with different ownership or resources: "
                        f"{json.dumps(mismatched, sort_keys=True)}"
                    )
                return {"status": "ok", "reused": True, "task": self._serialize_task(existing)}
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, agent_id, agent_alias, session_id, container_name,
                    requested_count, requested_devices, not_before, latest_start,
                    estimated_duration_seconds, preemptible, priority, state,
                    submitted_at, updated_at, message
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    task_id,
                    agent_id,
                    agent_alias,
                    session_id,
                    container_name,
                    count,
                    _json_devices(devices),
                    not_before,
                    latest_start,
                    duration,
                    1 if request.get("preemptible") else 0,
                    priority,
                    now,
                    now,
                    request.get("message"),
                ),
            )
            self._event(connection, "task-submitted", task_id=task_id, data={"count": count}, now=now)
            row = self._task_row(connection, task_id)
        return {"status": "queued", "reused": False, "task": self._serialize_task(row)}

    def add_hold(self, request: dict[str, Any], observed: dict[str, Any] | None = None) -> dict[str, Any]:
        now = self.clock()
        hold_id = require_safe_id(request.get("hold_id") or str(uuid.uuid4()), label="hold id")
        owner = require_safe_id(request.get("owner"), label="hold owner")
        devices = parse_devices(request.get("devices"), allow_none=False) or []
        not_before = parse_instant(request.get("not_before"))
        if not_before is None:
            not_before = now
        end_at = parse_instant(request.get("end_at"))
        if end_at is None:
            duration = int(request.get("duration_seconds") or 0)
            if duration < 1:
                raise CoordinationError("a hold requires end_at or duration_seconds >= 1")
            end_at = not_before + duration
        if end_at <= not_before:
            raise CoordinationError("hold end_at must be later than not_before")
        with self._transaction() as connection:
            existing = connection.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
            if existing is not None:
                expected = {
                    "owner": owner,
                    "devices": _json_devices(devices),
                    "not_before": not_before,
                    "end_at": end_at,
                }
                mismatched = {
                    key: {"existing": existing[key], "requested": value}
                    for key, value in expected.items()
                    if existing[key] != value
                }
                if mismatched:
                    raise CoordinationError(
                        f"hold id {hold_id} already exists with different ownership, devices, or window: "
                        f"{json.dumps(mismatched, sort_keys=True)}"
                    )
                return {"status": "ok", "reused": True, "hold": self._serialize_hold(existing)}
            connection.execute(
                """
                INSERT INTO holds(
                    hold_id, owner, devices, not_before, end_at, reason,
                    state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (hold_id, owner, _json_devices(devices), not_before, end_at, request.get("reason"), now, now),
            )
            self._event(connection, "hold-added", data={"hold_id": hold_id, "devices": devices}, now=now)
            conflicts: list[dict[str, Any]] = []
            for task in connection.execute(
                "SELECT * FROM tasks WHERE state IN ('granted','starting','active','orphaned_busy')"
            ).fetchall():
                overlap = sorted(set(devices) & set(_load_devices(task["granted_devices"])))
                if overlap:
                    conflicts.append({"task_id": task["task_id"], "devices": overlap, "state": task["state"]})
            busy = self._busy_set(observed)
            if busy:
                overlap = sorted(set(devices) & busy)
                if overlap:
                    conflicts.append({"external_busy": True, "devices": overlap})
            row = connection.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        return {
            "status": "recorded_with_conflicts" if conflicts else "recorded",
            "hold": self._serialize_hold(row),
            "conflicts": conflicts,
        }

    def remove_hold(self, hold_id: str) -> dict[str, Any]:
        hold_id = require_safe_id(hold_id, label="hold id")
        now = self.clock()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
            if row is None:
                raise CoordinationError(f"hold not found: {hold_id}")
            connection.execute(
                "UPDATE holds SET state='cancelled', updated_at=? WHERE hold_id=?",
                (now, hold_id),
            )
            self._event(connection, "hold-cancelled", data={"hold_id": hold_id}, now=now)
            row = connection.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        return {"status": "cancelled", "hold": self._serialize_hold(row)}

    def _housekeep(
        self,
        connection: sqlite3.Connection,
        observed: dict[str, Any] | None,
        *,
        now: float,
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        busy = self._busy_set(observed)
        visible = (
            {int(device) for device in observed.get("devices", [])}
            if observed is not None and observed.get("status") == "ok"
            else None
        )

        for row in connection.execute("SELECT * FROM holds WHERE state='active' AND end_at<=?", (now,)).fetchall():
            connection.execute("UPDATE holds SET state='expired', updated_at=? WHERE hold_id=?", (now, row["hold_id"]))
            changes.append({"hold_id": row["hold_id"], "from": "active", "to": "expired"})
            self._event(connection, "hold-expired", data={"hold_id": row["hold_id"]}, now=now)

        for row in connection.execute(
            "SELECT * FROM tasks WHERE state='queued' AND latest_start<=?", (now,)
        ).fetchall():
            connection.execute(
                "UPDATE tasks SET state='expired', updated_at=?, message=? WHERE task_id=?",
                (now, "latest_start elapsed before grant", row["task_id"]),
            )
            changes.append({"task_id": row["task_id"], "from": "queued", "to": "expired"})
            self._event(connection, "task-expired-in-queue", task_id=row["task_id"], now=now)

        deadline_rows = connection.execute(
            """
            SELECT * FROM tasks
            WHERE state IN ('granted','starting')
              AND activation_deadline IS NOT NULL
              AND activation_deadline<=?
            """,
            (now,),
        ).fetchall()
        for row in deadline_rows:
            devices = set(_load_devices(row["granted_devices"]))
            still_busy = (
                busy is None
                or visible is None
                or not devices.issubset(visible)
                or bool(devices & busy)
            )
            next_state = "orphaned_busy" if still_busy else "expired"
            message = (
                "activation deadline elapsed; occupancy still busy or unknown"
                if still_busy
                else "activation deadline elapsed before task became active"
            )
            connection.execute(
                "UPDATE tasks SET state=?, updated_at=?, message=? WHERE task_id=?",
                (next_state, now, message, row["task_id"]),
            )
            changes.append({"task_id": row["task_id"], "from": row["state"], "to": next_state})
            self._event(
                connection,
                "activation-deadline-elapsed",
                task_id=row["task_id"],
                data={"to": next_state},
                now=now,
            )

        heartbeat_rows = connection.execute(
            """
            SELECT * FROM tasks
            WHERE state='active'
              AND heartbeat_deadline IS NOT NULL
              AND heartbeat_deadline<=?
            """,
            (now,),
        ).fetchall()
        for row in heartbeat_rows:
            devices = set(_load_devices(row["granted_devices"]))
            still_busy = (
                busy is None
                or visible is None
                or not devices.issubset(visible)
                or bool(devices & busy)
            )
            next_state = "orphaned_busy" if still_busy else "released"
            message = (
                "heartbeat expired while hardware remained busy or unknown"
                if still_busy
                else "heartbeat expired and hardware was observed free"
            )
            connection.execute(
                "UPDATE tasks SET state=?, updated_at=?, message=? WHERE task_id=?",
                (next_state, now, message, row["task_id"]),
            )
            changes.append({"task_id": row["task_id"], "from": "active", "to": next_state})
            self._event(connection, "heartbeat-expired", task_id=row["task_id"], data={"to": next_state}, now=now)

        if busy is not None and visible is not None:
            for row in connection.execute("SELECT * FROM tasks WHERE state='orphaned_busy'").fetchall():
                devices = set(_load_devices(row["granted_devices"]))
                if devices.issubset(visible) and not devices.intersection(busy):
                    connection.execute(
                        "UPDATE tasks SET state='released', updated_at=?, message=? WHERE task_id=?",
                        (now, "orphaned task hardware is now free", row["task_id"]),
                    )
                    changes.append({"task_id": row["task_id"], "from": "orphaned_busy", "to": "released"})
                    self._event(connection, "orphaned-task-released", task_id=row["task_id"], now=now)

        for row in connection.execute(
            "SELECT * FROM tasks WHERE state='active' AND expected_end IS NOT NULL AND expected_end<=?",
            (now,),
        ).fetchall():
            if row["message"] != "estimated duration exceeded; lease remains protected":
                connection.execute(
                    "UPDATE tasks SET updated_at=?, message=? WHERE task_id=?",
                    (now, "estimated duration exceeded; lease remains protected", row["task_id"]),
                )
                changes.append({"task_id": row["task_id"], "state": "active", "overdue": True})
                self._event(connection, "task-overdue", task_id=row["task_id"], now=now)
        return changes

    @staticmethod
    def _reserved_devices(connection: sqlite3.Connection, *, exclude_task: str | None = None) -> set[int]:
        reserved: set[int] = set()
        query = (
            "SELECT task_id, granted_devices FROM tasks "
            "WHERE state IN ('granted','starting','active','orphaned_busy')"
        )
        for row in connection.execute(query).fetchall():
            if exclude_task and row["task_id"] == exclude_task:
                continue
            reserved.update(_load_devices(row["granted_devices"]))
        return reserved

    @staticmethod
    def _hold_conflicts(
        connection: sqlite3.Connection,
        *,
        device: int,
        start_at: float,
        end_at: float,
    ) -> bool:
        for row in connection.execute(
            """
            SELECT devices FROM holds
            WHERE state='active' AND not_before<? AND end_at>?
            """,
            (end_at, start_at),
        ).fetchall():
            if device in _load_devices(row["devices"]):
                return True
        return False

    def acquire(
        self,
        task_id: str,
        observed: dict[str, Any],
        *,
        grant_ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
    ) -> dict[str, Any]:
        task_id = require_safe_id(task_id, label="task id")
        if grant_ttl_seconds < 1:
            raise CoordinationError("grant_ttl_seconds must be >= 1")
        if observed.get("status") != "ok":
            return {"status": "probe_failed", "error": observed.get("error"), "occupancy": observed}
        now = self.clock()
        busy = self._busy_set(observed) or set()
        with self._transaction() as connection:
            changes = self._housekeep(connection, observed, now=now)
            row = self._task_row(connection, task_id)
            if row["state"] != "queued":
                return {"status": row["state"], "task": self._serialize_task(row), "gc": changes}
            if float(row["not_before"]) > now:
                return {"status": "waiting", "reason": "not_before", "task": self._serialize_task(row), "gc": changes}
            head = connection.execute(
                """
                SELECT * FROM tasks
                WHERE state='queued' AND not_before<=? AND latest_start>?
                ORDER BY priority DESC, submitted_at ASC, task_id ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if head is None or head["task_id"] != task_id:
                return {
                    "status": "waiting",
                    "reason": "strict_fifo",
                    "ahead_task_id": None if head is None else head["task_id"],
                    "task": self._serialize_task(row),
                    "gc": changes,
                }
            reserved = self._reserved_devices(connection, exclude_task=task_id)
            estimated_end = now + int(row["estimated_duration_seconds"])
            available = []
            for device in observed.get("devices", []):
                device = int(device)
                if device in busy or device in reserved:
                    continue
                if self._hold_conflicts(connection, device=device, start_at=now, end_at=estimated_end):
                    continue
                available.append(device)
            requested = _load_devices(row["requested_devices"])
            if requested:
                missing = sorted(set(requested) - set(available))
                if missing:
                    return {
                        "status": "waiting",
                        "reason": "requested_devices_unavailable",
                        "unavailable_devices": missing,
                        "available_devices": sorted(available),
                        "task": self._serialize_task(row),
                        "gc": changes,
                    }
                selected = requested
            else:
                count = int(row["requested_count"])
                if len(available) < count:
                    return {
                        "status": "waiting",
                        "reason": "not_enough_devices",
                        "needed": count,
                        "available_devices": sorted(available),
                        "task": self._serialize_task(row),
                        "gc": changes,
                    }
                selected = sorted(available)[:count]
            fence = self._next_fence(connection)
            deadline = now + grant_ttl_seconds
            connection.execute(
                """
                UPDATE tasks
                SET state='granted', granted_devices=?, activation_deadline=?,
                    fence_token=?, updated_at=?, message=?
                WHERE task_id=?
                """,
                (_json_devices(selected), deadline, fence, now, "short-lived grant issued", task_id),
            )
            self._event(
                connection,
                "task-granted",
                task_id=task_id,
                data={"devices": selected, "fence_token": fence, "deadline": utc_now_iso(deadline)},
                now=now,
            )
            row = self._task_row(connection, task_id)
        return {
            "status": "granted",
            "task": self._serialize_task(row),
            "environment": {"ASCEND_RT_VISIBLE_DEVICES": ",".join(str(item) for item in selected)},
            "occupancy": observed,
            "gc": changes,
        }

    def preflight(
        self,
        task_id: str,
        token: int,
        observed: dict[str, Any],
        *,
        start_ttl_seconds: int = DEFAULT_START_TTL_SECONDS,
    ) -> dict[str, Any]:
        task_id = require_safe_id(task_id, label="task id")
        if start_ttl_seconds < 1:
            raise CoordinationError("start_ttl_seconds must be >= 1")
        if observed.get("status") != "ok":
            return {"status": "probe_failed", "error": observed.get("error"), "occupancy": observed}
        now = self.clock()
        busy = self._busy_set(observed) or set()
        visible = {int(device) for device in observed.get("devices", [])}
        with self._transaction() as connection:
            changes = self._housekeep(connection, observed, now=now)
            row = self._task_row(connection, task_id)
            self._check_token(row, token)
            if row["state"] != "granted":
                raise CoordinationError(f"task {task_id} is {row['state']}, expected granted")
            devices = _load_devices(row["granted_devices"])
            conflicts = sorted((set(devices) & busy) | (set(devices) - visible))
            if conflicts:
                next_state = "queued" if float(row["latest_start"]) > now else "expired"
                connection.execute(
                    """
                    UPDATE tasks
                    SET state=?, granted_devices=NULL, activation_deadline=NULL,
                        fence_token=NULL, updated_at=?, message=?
                    WHERE task_id=?
                    """,
                    (next_state, now, f"preflight found externally busy devices: {conflicts}", task_id),
                )
                self._event(
                    connection,
                    "preflight-conflict",
                    task_id=task_id,
                    data={"devices": conflicts, "to": next_state},
                    now=now,
                )
                row = self._task_row(connection, task_id)
                return {
                    "status": "waiting" if next_state == "queued" else "expired",
                    "reason": "external_occupancy",
                    "conflicting_devices": conflicts,
                    "task": self._serialize_task(row),
                    "gc": changes,
                }
            deadline = now + start_ttl_seconds
            connection.execute(
                "UPDATE tasks SET state='starting', activation_deadline=?, updated_at=?, message=? WHERE task_id=?",
                (deadline, now, "preflight passed; launch before activation deadline", task_id),
            )
            self._event(connection, "preflight-passed", task_id=task_id, data={"devices": devices}, now=now)
            row = self._task_row(connection, task_id)
        return {
            "status": "starting",
            "task": self._serialize_task(row),
            "environment": {"ASCEND_RT_VISIBLE_DEVICES": ",".join(str(item) for item in devices)},
            "occupancy": observed,
            "gc": changes,
        }

    def activate(
        self,
        task_id: str,
        token: int,
        *,
        pid: int,
        heartbeat_ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
    ) -> dict[str, Any]:
        task_id = require_safe_id(task_id, label="task id")
        if pid < 1:
            raise CoordinationError("pid must be >= 1")
        if heartbeat_ttl_seconds < 1:
            raise CoordinationError("heartbeat_ttl_seconds must be >= 1")
        now = self.clock()
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            self._check_token(row, token)
            if row["state"] != "starting":
                raise CoordinationError(f"task {task_id} is {row['state']}, expected starting")
            if row["activation_deadline"] is not None and float(row["activation_deadline"]) <= now:
                raise CoordinationError(f"activation deadline elapsed for task {task_id}")
            expected_end = now + int(row["estimated_duration_seconds"])
            heartbeat_deadline = now + heartbeat_ttl_seconds
            connection.execute(
                """
                UPDATE tasks
                SET state='active', pid=?, started_at=?, expected_end=?,
                    heartbeat_at=?, heartbeat_deadline=?, activation_deadline=NULL,
                    updated_at=?, message=?
                WHERE task_id=?
                """,
                (pid, now, expected_end, now, heartbeat_deadline, now, "task reported active", task_id),
            )
            self._event(connection, "task-activated", task_id=task_id, data={"pid": pid}, now=now)
            row = self._task_row(connection, task_id)
        return {"status": "active", "task": self._serialize_task(row)}

    def heartbeat(
        self,
        task_id: str,
        token: int,
        *,
        heartbeat_ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
    ) -> dict[str, Any]:
        task_id = require_safe_id(task_id, label="task id")
        if heartbeat_ttl_seconds < 1:
            raise CoordinationError("heartbeat_ttl_seconds must be >= 1")
        now = self.clock()
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            self._check_token(row, token)
            if row["state"] not in {"active", "orphaned_busy"}:
                raise CoordinationError(f"task {task_id} is {row['state']}, expected active or orphaned_busy")
            connection.execute(
                """
                UPDATE tasks
                SET state='active', heartbeat_at=?, heartbeat_deadline=?,
                    updated_at=?, message=?
                WHERE task_id=?
                """,
                (now, now + heartbeat_ttl_seconds, now, "heartbeat received", task_id),
            )
            row = self._task_row(connection, task_id)
        return {"status": "active", "task": self._serialize_task(row)}

    def release(self, task_id: str, token: int, observed: dict[str, Any]) -> dict[str, Any]:
        task_id = require_safe_id(task_id, label="task id")
        now = self.clock()
        busy = self._busy_set(observed)
        visible = (
            {int(device) for device in observed.get("devices", [])}
            if observed.get("status") == "ok"
            else None
        )
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            self._check_token(row, token)
            devices = set(_load_devices(row["granted_devices"]))
            conflicts = (
                sorted((devices & busy) | (devices - visible))
                if busy is not None and visible is not None
                else sorted(devices)
            )
            if busy is None or visible is None or conflicts:
                connection.execute(
                    "UPDATE tasks SET state='orphaned_busy', updated_at=?, message=? WHERE task_id=?",
                    (now, "release requested but hardware remained busy or unknown", task_id),
                )
                self._event(connection, "release-deferred", task_id=task_id, data={"devices": conflicts}, now=now)
                row = self._task_row(connection, task_id)
                return {
                    "status": "orphaned_busy",
                    "conflicting_devices": conflicts,
                    "task": self._serialize_task(row),
                    "occupancy": observed,
                }
            connection.execute(
                "UPDATE tasks SET state='released', updated_at=?, message=? WHERE task_id=?",
                (now, "hardware observed free; cooperative lease released", task_id),
            )
            self._event(connection, "task-released", task_id=task_id, now=now)
            row = self._task_row(connection, task_id)
        return {"status": "released", "task": self._serialize_task(row), "occupancy": observed}

    def cancel(self, task_id: str, observed: dict[str, Any] | None = None) -> dict[str, Any]:
        task_id = require_safe_id(task_id, label="task id")
        now = self.clock()
        busy = self._busy_set(observed)
        visible = (
            {int(device) for device in observed.get("devices", [])}
            if observed is not None and observed.get("status") == "ok"
            else None
        )
        with self._transaction() as connection:
            row = self._task_row(connection, task_id)
            if row["state"] in {"released", "expired", "cancelled"}:
                return {"status": row["state"], "task": self._serialize_task(row)}
            devices = set(_load_devices(row["granted_devices"]))
            still_busy = bool(devices) and (
                busy is None
                or visible is None
                or not devices.issubset(visible)
                or bool(devices & busy)
            )
            next_state = "orphaned_busy" if still_busy else "cancelled"
            connection.execute(
                "UPDATE tasks SET state=?, updated_at=?, message=? WHERE task_id=?",
                (next_state, now, "cancel requested", task_id),
            )
            self._event(connection, "task-cancelled", task_id=task_id, data={"to": next_state}, now=now)
            row = self._task_row(connection, task_id)
        return {"status": next_state, "task": self._serialize_task(row)}

    def snapshot(
        self,
        observed: dict[str, Any] | None,
        *,
        task_id: str | None = None,
        event_limit: int = 50,
    ) -> dict[str, Any]:
        now = self.clock()
        with self._transaction() as connection:
            changes = self._housekeep(connection, observed, now=now)
            meta = {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}
            if task_id:
                task_id = require_safe_id(task_id, label="task id")
                tasks = [self._task_row(connection, task_id)]
            else:
                tasks = connection.execute(
                    "SELECT * FROM tasks ORDER BY submitted_at ASC, task_id ASC"
                ).fetchall()
            holds = connection.execute("SELECT * FROM holds ORDER BY not_before ASC, hold_id ASC").fetchall()
            events = connection.execute(
                "SELECT * FROM events ORDER BY event_id DESC LIMIT ?", (max(0, event_limit),)
            ).fetchall()
        return {
            "status": "ok",
            "schema_version": int(meta.get("schema_version", SCHEMA_VERSION)),
            "coordination_epoch": meta.get("coordination_epoch"),
            "state_dir": str(self.state_dir),
            "database": str(self.db_path),
            "ephemeral": True,
            "tasks": [self._serialize_task(row) for row in tasks],
            "holds": [self._serialize_hold(row) for row in holds],
            "events": [
                {
                    "event_id": row["event_id"],
                    "at": utc_now_iso(float(row["at"])),
                    "kind": row["kind"],
                    "task_id": row["task_id"],
                    "data": json.loads(row["data"]),
                }
                for row in events
            ],
            "occupancy": observed,
            "gc": changes,
        }


def _confirmed_free_probe(
    *,
    samples: int,
    interval_seconds: float,
    probe: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    if samples < 1:
        raise CoordinationError("free_samples must be >= 1")
    observations: list[dict[str, Any]] = []
    for index in range(samples):
        if index:
            time.sleep(interval_seconds)
        observations.append(probe())
    latest = dict(observations[-1])
    failed = [item for item in observations if item.get("status") != "ok"]
    if failed:
        latest.update(
            {
                "status": "failed",
                "error": "one or more release confirmation probes failed",
                "busy": {},
                "free": [],
            }
        )
    else:
        # A device is releasable only when every confirmation sample observed
        # it free.  Unioning busy reasons makes one transient busy sample keep
        # the cooperative lease protected.
        combined_busy: dict[str, list[dict[str, Any]]] = {}
        for item in observations:
            for device, reasons in item.get("busy", {}).items():
                combined_busy.setdefault(str(device), []).extend(reasons)
        latest["busy"] = combined_busy
        latest["free"] = sorted(
            int(device)
            for device in latest.get("devices", [])
            if str(device) not in combined_busy
        )
    if samples > 1:
        latest["confirmation_samples"] = observations
        latest["confirmation_sample_count"] = samples
    return latest


def handle_request(
    request: dict[str, Any],
    *,
    probe: Callable[[], dict[str, Any]] = probe_npu_occupancy,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Execute one structured coordinator request on the host."""
    action = request.get("action")
    coordinator = NpuCoordinator(request.get("state_dir") or DEFAULT_STATE_DIR, clock=clock)
    if action == "submit":
        return coordinator.submit(request)
    if action == "acquire":
        return coordinator.acquire(
            request["task_id"],
            probe(),
            grant_ttl_seconds=int(request.get("grant_ttl_seconds") or DEFAULT_GRANT_TTL_SECONDS),
        )
    if action == "preflight":
        return coordinator.preflight(
            request["task_id"],
            int(request["fence_token"]),
            probe(),
            start_ttl_seconds=int(request.get("start_ttl_seconds") or DEFAULT_START_TTL_SECONDS),
        )
    if action == "activate":
        return coordinator.activate(
            request["task_id"],
            int(request["fence_token"]),
            pid=int(request["pid"]),
            heartbeat_ttl_seconds=int(
                request.get("heartbeat_ttl_seconds") or DEFAULT_HEARTBEAT_TTL_SECONDS
            ),
        )
    if action == "heartbeat":
        return coordinator.heartbeat(
            request["task_id"],
            int(request["fence_token"]),
            heartbeat_ttl_seconds=int(
                request.get("heartbeat_ttl_seconds") or DEFAULT_HEARTBEAT_TTL_SECONDS
            ),
        )
    if action == "release":
        observed = _confirmed_free_probe(
            samples=int(request.get("free_samples") or 2),
            interval_seconds=float(request.get("interval_seconds") or 2.0),
            probe=probe,
        )
        return coordinator.release(request["task_id"], int(request["fence_token"]), observed)
    if action == "cancel":
        return coordinator.cancel(request["task_id"], probe())
    if action == "hold-add":
        return coordinator.add_hold(request, probe())
    if action == "hold-remove":
        return coordinator.remove_hold(request["hold_id"])
    if action in {"status", "gc"}:
        observed = None if request.get("no_probe") else probe()
        return coordinator.snapshot(
            observed,
            task_id=request.get("task_id"),
            event_limit=int(request.get("event_limit") or 50),
        )
    raise CoordinationError(f"unsupported action: {action!r}")
