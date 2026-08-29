from __future__ import annotations

import json
import mimetypes
import re
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .agent_view import AgentQueryError, capacity_candidates, compact_server, find_server, npu_status, server_status
from .db import Database
from .scheduler import AdaptiveScheduler
from .settings import Settings
from .workspace_adapter import WorkspaceDeviceAdapter


RANGES = {
    "1h": (3600, 60), "6h": (21600, 300), "24h": (86400, 600),
    "7d": (604800, 3600), "30d": (2592000, 14400), "90d": (7776000, 43200),
}


def normalize_tags(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 20 or not all(isinstance(tag, str) for tag in value):
        raise ValueError("tags 必须是最多包含 20 个字符串的数组")
    tags: list[str] = []
    seen: set[str] = set()
    for value_tag in value:
        tag = value_tag.strip()[:60]
        key = tag.casefold()
        if tag and key not in seen:
            tags.append(tag)
            seen.add(key)
    return tags


class App:
    def __init__(self, settings: Settings, db: Database, adapter: WorkspaceDeviceAdapter, scheduler: AdaptiveScheduler) -> None:
        self.settings = settings
        self.db = db
        self.adapter = adapter
        self.scheduler = scheduler
        self.web_root = settings.project_root / "dist" / "client"

    def overview(self) -> dict[str, Any]:
        servers = self.db.list_servers()
        snapshots = self.scheduler.snapshots()
        rows = []
        totals = {
            "servers": len(servers), "online_servers": 0, "npu_count": 0,
            "busy_npu_count": 0, "hbm_used_mb": 0, "hbm_total_mb": 0,
            "npu_util_percent": None,
        }
        utils: list[float] = []
        for server in servers:
            snapshot = snapshots.get(server["id"])
            status = snapshot.get("status") if snapshot else ("offline" if server.get("last_error") else "pending")
            row = {**server, "status": status, "snapshot": snapshot}
            rows.append(row)
            if status == "online" and snapshot:
                totals["online_servers"] += 1
                summary = snapshot.get("summary", {})
                totals["npu_count"] += summary.get("npu_count") or 0
                totals["busy_npu_count"] += summary.get("busy_npu_count") or 0
                totals["hbm_used_mb"] += summary.get("hbm_used_mb") or 0
                totals["hbm_total_mb"] += summary.get("hbm_total_mb") or 0
                if summary.get("npu_util_percent") is not None:
                    utils.append(float(summary["npu_util_percent"]))
        totals["npu_util_percent"] = round(sum(utils) / len(utils), 1) if utils else None
        totals["idle_npu_count"] = totals["npu_count"] - totals["busy_npu_count"]
        return {"generated_at": int(time.time()), "totals": totals, "servers": rows, "runtime": self.scheduler.runtime_state()}

    def agent_servers(self) -> dict[str, Any]:
        snapshots = self.scheduler.snapshots()
        return {
            "source": "cache",
            "servers": [compact_server(server, snapshots.get(server["id"])) for server in self.db.list_servers()],
        }

    def _agent_snapshot(
        self, host: str, mode: str, *, force_infrastructure: bool = False, timeout: int = 30,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        snapshots = self.scheduler.snapshots()
        server, snapshot = find_server(host, self.db.list_servers(), snapshots)
        if mode not in ("cache", "live"):
            raise AgentQueryError("mode must be cache or live")
        if mode == "live":
            if not server.get("enabled"):
                raise AgentQueryError("live collection is disabled for this server", 409)
            try:
                snapshot = self.scheduler.collect_and_wait(
                    server["id"], force_infrastructure=force_infrastructure, timeout=timeout,
                )
            except TimeoutError as exc:
                raise AgentQueryError(str(exc), 504) from exc
        return server, snapshot

    def agent_npu(
        self, host: str, include_processes: bool = False, detailed_processes: bool = False,
        mode: str = "cache", timeout: int = 30,
    ) -> dict[str, Any]:
        server, snapshot = self._agent_snapshot(host, mode, timeout=timeout)
        return npu_status(
            server, snapshot, include_processes=include_processes, detailed_processes=detailed_processes,
        )

    def agent_server(
        self, host: str, mode: str = "cache", include_processes: bool = True,
        detailed_processes: bool = False, timeout: int = 30,
    ) -> dict[str, Any]:
        server, snapshot = self._agent_snapshot(host, mode, force_infrastructure=mode == "live", timeout=timeout)
        return server_status(
            server, snapshot, include_processes=include_processes,
            detailed_processes=detailed_processes, include_infrastructure=True,
        )

    def agent_capacity(
        self, min_idle_npus: int, max_age_seconds: int, tags: list[str], include_disabled: bool,
    ) -> dict[str, Any]:
        if not 0 <= min_idle_npus <= 64 or not 0 <= max_age_seconds <= 86400:
            raise AgentQueryError("capacity limits are out of range")
        return capacity_candidates(
            self.db.list_servers(), self.scheduler.snapshots(), min_idle_npus=min_idle_npus,
            max_age_seconds=max_age_seconds, tags=tags, include_disabled=include_disabled,
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "NPUFleetMonitor/0.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/api/viewers/"):
            return
        super().log_message(fmt, *args)

    def _headers(self, status: int, content_type: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "public, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        origin = self.headers.get("Origin", "")
        if re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def json_response(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 1024 * 1024:
            raise ValueError("请求体为空或超过 1 MB")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("请求体不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self.json_response({"status": "ok", "version": "0.1.0", "runtime": self.app.scheduler.runtime_state()})
        if parsed.path == "/api/overview":
            return self.json_response(self.app.overview())
        if parsed.path == "/api/agent/servers":
            return self.json_response(self.app.agent_servers())
        if parsed.path == "/api/agent/npu":
            query = parse_qs(parsed.query)
            host = query.get("host", [""])[0]
            include_processes = query.get("processes", ["0"])[0] in ("1", "true", "yes")
            detailed_processes = query.get("details", ["0"])[0] in ("1", "true", "yes")
            try:
                timeout = int(query.get("timeout", ["30"])[0])
                return self.json_response(self.app.agent_npu(
                    host, include_processes, detailed_processes, query.get("mode", ["cache"])[0], timeout,
                ))
            except AgentQueryError as exc:
                return self.json_response({"error": str(exc)}, exc.status)
            except ValueError:
                return self.json_response({"error": "timeout must be an integer"}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/agent/server":
            query = parse_qs(parsed.query)
            try:
                return self.json_response(self.app.agent_server(
                    query.get("host", [""])[0], query.get("mode", ["cache"])[0],
                    query.get("processes", ["1"])[0] in ("1", "true", "yes"),
                    query.get("details", ["0"])[0] in ("1", "true", "yes"),
                    int(query.get("timeout", ["30"])[0]),
                ))
            except AgentQueryError as exc:
                return self.json_response({"error": str(exc)}, exc.status)
            except ValueError:
                return self.json_response({"error": "timeout must be an integer"}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/agent/capacity":
            query = parse_qs(parsed.query)
            try:
                tags = [tag.strip() for tag in query.get("tags", [""])[0].split(",") if tag.strip()]
                return self.json_response(self.app.agent_capacity(
                    int(query.get("min_idle_npus", ["1"])[0]),
                    int(query.get("max_age_seconds", ["300"])[0]), tags,
                    query.get("include_disabled", ["0"])[0] in ("1", "true", "yes"),
                ))
            except AgentQueryError as exc:
                return self.json_response({"error": str(exc)}, exc.status)
            except ValueError:
                return self.json_response({"error": "capacity parameters must be integers"}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/servers":
            return self.json_response({"servers": self.app.db.list_servers()})
        if parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            range_name = query.get("range", ["24h"])[0]
            seconds, bucket = RANGES.get(range_name, RANGES["24h"])
            server_id = query.get("server_id", [None])[0]
            return self.json_response({
                "range": range_name, "bucket_seconds": bucket,
                "points": self.app.db.history(server_id, int(time.time()) - seconds, bucket),
            })
        if parsed.path == "/api/history/heatmap":
            query = parse_qs(parsed.query)
            range_name = query.get("range", ["7d"])[0]
            seconds, _ = RANGES.get(range_name, RANGES["7d"])
            server_id = query.get("server_id", [None])[0]
            if not server_id:
                return self.json_response({"error": "server_id is required"}, HTTPStatus.BAD_REQUEST)
            try:
                timezone_offset = int(query.get("timezone_offset", ["0"])[0])
            except ValueError:
                return self.json_response({"error": "timezone_offset must be an integer"}, HTTPStatus.BAD_REQUEST)
            return self.json_response({
                "range": range_name,
                "bucket_seconds": 7200,
                "points": self.app.db.history_heatmap(
                    server_id, int(time.time()) - seconds, 7200, timezone_offset,
                ),
            })
        return self._static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/servers/batch":
                return self._batch_servers(self.json_body())
            match = re.fullmatch(r"/api/servers/([^/]+)/collect", parsed.path)
            if match:
                self.app.scheduler.collect_now(match.group(1))
                return self.json_response({"accepted": True}, HTTPStatus.ACCEPTED)
            if parsed.path == "/api/collect":
                self.app.scheduler.collect_now()
                return self.json_response({"accepted": True}, HTTPStatus.ACCEPTED)
        except ValueError as exc:
            return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            return self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            viewer = re.fullmatch(r"/api/viewers/([A-Za-z0-9_-]{8,80})", parsed.path)
            if viewer:
                body = self.json_body()
                state = self.app.scheduler.heartbeat(
                    viewer.group(1), int(body.get("interval", 10)), bool(body.get("visible", True)),
                )
                return self.json_response(state)
            server = re.fullmatch(r"/api/servers/([^/]+)", parsed.path)
            if server:
                body = self.json_body()
                enabled = None
                tags = None
                if "enabled" in body:
                    if not isinstance(body["enabled"], bool):
                        raise ValueError("enabled 必须是布尔值")
                    enabled = body["enabled"]
                if "tags" in body:
                    tags = normalize_tags(body["tags"])
                if enabled is None and tags is None:
                    raise ValueError("至少提供 enabled 或 tags")
                if not self.app.db.update_server(server.group(1), enabled=enabled, tags=tags):
                    return self.json_response({"error": "server not found"}, HTTPStatus.NOT_FOUND)
                return self.json_response({"ok": True, "server": self.app.db.get_server(server.group(1))})
        except ValueError as exc:
            return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        viewer = re.fullmatch(r"/api/viewers/([A-Za-z0-9_-]{8,80})", parsed.path)
        if viewer:
            self.app.scheduler.remove_lease(viewer.group(1))
            return self.json_response({"ok": True})
        server = re.fullmatch(r"/api/servers/([^/]+)", parsed.path)
        if server:
            deleted = self.app.db.delete_server(server.group(1))
            return self.json_response({"ok": deleted}, 200 if deleted else HTTPStatus.NOT_FOUND)
        self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _batch_servers(self, body: dict[str, Any]) -> None:
        entries = body.get("servers")
        passwords = body.get("passwords") or []
        if not isinstance(entries, list) or not entries or len(entries) > 200:
            raise ValueError("servers 必须包含 1 到 200 个条目")
        if not isinstance(passwords, list) or len(passwords) > 20 or not all(isinstance(item, str) for item in passwords):
            raise ValueError("passwords 最多包含 20 个字符串候选")
        results = []
        for entry in entries:
            try:
                if not isinstance(entry, dict):
                    raise ValueError("服务器条目必须是对象")
                host = str(entry.get("host", "")).strip()
                port = int(entry.get("port", 22))
                username = str(entry.get("username", "root")).strip()
                self.app.adapter.validate_endpoint(host, port, username)
                server = self.app.db.upsert_server({
                    "id": uuid.uuid4().hex, "name": str(entry.get("name") or host).strip()[:120],
                    "host": host, "port": port, "username": username,
                    "tags": normalize_tags(entry.get("tags", [])),
                })
                auth = self.app.adapter.bootstrap_with_passwords(server, passwords)
                if auth["ok"]:
                    self.app.scheduler.collect_now(server["id"])
                else:
                    self.app.db.record_failure(server["id"], str(auth.get("error")), 0)
                results.append({"server": server, "auth": auth})
            except Exception as exc:  # noqa: BLE001
                results.append({"server": entry, "auth": {"ok": False, "error": str(exc)}})
        self.json_response({"results": results}, HTTPStatus.MULTI_STATUS)

    def _static(self, request_path: str) -> None:
        root = self.app.web_root
        if not root.is_dir():
            return self.json_response({"error": "前端尚未构建，请先运行 npm run build"}, HTTPStatus.SERVICE_UNAVAILABLE)
        relative = request_path.lstrip("/") or "index.html"
        candidate = (root / relative).resolve()
        if root not in candidate.parents and candidate != root:
            return self.json_response({"error": "invalid path"}, HTTPStatus.BAD_REQUEST)
        if not candidate.is_file():
            candidate = root / "index.html"
        if not candidate.is_file():
            return self.json_response({"error": "index.html not found"}, HTTPStatus.NOT_FOUND)
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._headers(200, content_type, len(body))
        self.wfile.write(body)


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: App) -> None:
        super().__init__(address, Handler)
        self.app = app
