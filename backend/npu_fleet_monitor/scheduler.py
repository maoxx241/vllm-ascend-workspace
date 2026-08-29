from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .db import Database
from .probe import HostProbe
from .settings import Settings


ALLOWED_INTERVALS = (1, 5, 10, 30)


class AdaptiveScheduler:
    def __init__(self, settings: Settings, db: Database, probe: HostProbe) -> None:
        self.settings = settings
        self.db = db
        self.probe = probe
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._leases: dict[str, tuple[int, float]] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._latest_persisted = db.latest_persisted()
        self._latest_failure_event: dict[str, int] = {}
        self._last_infra: dict[str, float] = {}
        self._last_cycle_at: int | None = None
        self._cycle_duration_ms: int | None = None
        self._collecting = False
        self._manual: set[str] = set()
        self._force_infrastructure: set[str] = set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="nfm-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=5)

    def heartbeat(self, client_id: str, interval: int, visible: bool) -> dict[str, Any]:
        if interval not in ALLOWED_INTERVALS:
            raise ValueError(f"刷新频率必须是 {ALLOWED_INTERVALS} 之一")
        with self._condition:
            if visible:
                self._leases[client_id] = (interval, time.monotonic() + max(20, interval * 4))
            else:
                self._leases.pop(client_id, None)
            self._condition.notify_all()
        return self.runtime_state()

    def remove_lease(self, client_id: str) -> None:
        with self._condition:
            self._leases.pop(client_id, None)
            self._condition.notify_all()

    def collect_now(self, server_id: str | None = None, force_infrastructure: bool = False) -> None:
        with self._condition:
            self._manual.add(server_id or "*")
            if force_infrastructure:
                self._force_infrastructure.add(server_id or "*")
            self._condition.notify_all()

    def collect_and_wait(
        self,
        server_id: str,
        *,
        force_infrastructure: bool = False,
        timeout: float = 30,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(1, min(float(timeout), 60))
        with self._condition:
            previous = self._snapshots.get(server_id)
            self._manual.add(server_id)
            if force_infrastructure:
                self._force_infrastructure.add(server_id)
            self._condition.notify_all()
            while not self._stop.is_set():
                current = self._snapshots.get(server_id)
                if current is not None and current is not previous:
                    return dict(current)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"live collection timed out after {timeout:g}s")
                self._condition.wait(timeout=remaining)
        raise RuntimeError("collector is stopped")

    def _active_intervals(self) -> list[int]:
        now = time.monotonic()
        expired = [client for client, (_, expires) in self._leases.items() if expires <= now]
        for client in expired:
            self._leases.pop(client, None)
        return [value[0] for value in self._leases.values()]

    def effective_interval(self) -> int:
        with self._condition:
            active = self._active_intervals()
            return min(active) if active else self.settings.idle_interval

    def runtime_state(self) -> dict[str, Any]:
        with self._condition:
            active = self._active_intervals()
            return {
                "mode": "interactive" if active else "idle",
                "effective_interval": min(active) if active else self.settings.idle_interval,
                "idle_interval": self.settings.idle_interval,
                "history_interval": self.settings.history_interval,
                "active_viewers": len(active),
                "collecting": self._collecting,
                "last_cycle_at": self._last_cycle_at,
                "cycle_duration_ms": self._cycle_duration_ms,
                "allowed_intervals": list(ALLOWED_INTERVALS),
            }

    def snapshots(self) -> dict[str, dict[str, Any]]:
        with self._condition:
            return dict(self._snapshots)

    def _run(self) -> None:
        next_cycle = 0.0
        prune_at = 0.0
        while not self._stop.is_set():
            with self._condition:
                interval = self.effective_interval()
                now = time.monotonic()
                manual = bool(self._manual)
                wait_for = max(0.0, next_cycle - now)
                if not manual and wait_for > 0:
                    self._condition.wait(timeout=min(wait_for, 5.0))
                    continue
                targets = set(self._manual)
                self._manual.clear()
                force_infrastructure = set(self._force_infrastructure)
                self._force_infrastructure.clear()
                self._collecting = True
            started = time.monotonic()
            try:
                self._collect_cycle(targets, force_infrastructure)
            finally:
                with self._condition:
                    self._collecting = False
                    self._last_cycle_at = int(time.time())
                    self._cycle_duration_ms = round((time.monotonic() - started) * 1000)
                next_cycle = time.monotonic() + self.effective_interval()
            if time.monotonic() >= prune_at:
                self.db.prune(self.settings.retention_days)
                prune_at = time.monotonic() + 86400

    def _collect_cycle(self, targets: set[str], force_infrastructure: set[str] | None = None) -> None:
        force_infrastructure = force_infrastructure or set()
        servers = [server for server in self.db.list_servers() if server["enabled"]]
        if targets and "*" not in targets:
            servers = [server for server in servers if server["id"] in targets]
        if not servers:
            return
        now = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(self.settings.max_workers, len(servers))) as pool:
            futures = {}
            for server in servers:
                include_infra = (
                    "*" in force_infrastructure
                    or server["id"] in force_infrastructure
                    or now - self._last_infra.get(server["id"], 0) >= self.settings.infrastructure_interval
                )
                futures[pool.submit(self.probe.collect, server, include_infra)] = (server, include_infra)
            for future in as_completed(futures):
                server, include_infra = futures[future]
                try:
                    snapshot = future.result()
                except Exception as exc:  # noqa: BLE001
                    failed_at = int(time.time())
                    persist_failure = failed_at - self._latest_failure_event.get(server["id"], 0) >= self.settings.history_interval
                    self.db.record_failure(server["id"], str(exc), 0, persist_failure)
                    if persist_failure:
                        self._latest_failure_event[server["id"]] = failed_at
                    with self._condition:
                        self._snapshots[server["id"]] = {
                            "server_id": server["id"], "collected_at": int(time.time()),
                            "status": "offline", "error": str(exc)[-1200:],
                        }
                        self._condition.notify_all()
                    continue
                if include_infra:
                    self._last_infra[server["id"]] = time.monotonic()
                last_persisted = self._latest_persisted.get(server["id"], 0)
                persist = int(snapshot["collected_at"]) - last_persisted >= self.settings.history_interval
                self.db.record_success(server["id"], snapshot, persist)
                if persist:
                    self._latest_persisted[server["id"]] = int(snapshot["collected_at"])
                snapshot["status"] = "online"
                with self._condition:
                    self._snapshots[server["id"]] = snapshot
                    self._condition.notify_all()
