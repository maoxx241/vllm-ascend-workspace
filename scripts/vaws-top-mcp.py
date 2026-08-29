#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from typing import Any

from vaws_top_client import ClientError, VawsTopClient, format_capacity, format_mounts, format_npu, format_server, format_servers


LEGACY_VERSION = "2025-11-25"
MODERN_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = [MODERN_VERSION, LEGACY_VERSION, "2025-06-18", "2025-03-26", "2024-11-05"]


TOOLS = [
    {
        "name": "npu_status",
        "title": "NPU status",
        "description": "Get NPU usage by IP or hostname. Cache mode returns immediately; live mode asks the central collector for one fresh probe.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Server IP, hostname, display name, or id"},
                "mode": {"type": "string", "enum": ["cache", "live"], "default": "cache"},
                "include_processes": {"type": "boolean", "default": False},
                "process_details": {"type": "boolean", "default": False, "description": "Include pwd and launch command"},
                "max_age_seconds": {"type": "integer", "minimum": 0},
            },
            "required": ["host"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "server_status",
        "title": "Server experiment status",
        "description": "Inspect cached or live NPU, CPU, memory, disks, Docker containers, processes, and likely owners before choosing a server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "mode": {"type": "string", "enum": ["cache", "live"], "default": "cache"},
                "process_details": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 30},
            },
            "required": ["host"], "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "list_mounts",
        "title": "Server mounts",
        "description": "List mounted filesystems, capacity, and likely model-weight mount points from cache or a live infrastructure probe.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "mode": {"type": "string", "enum": ["cache", "live"], "default": "cache"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 30},
            },
            "required": ["host"], "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "find_npu_capacity",
        "title": "Find NPU capacity",
        "description": "Rank fresh cached servers that satisfy idle-NPU and tag requirements; low-priority hosts sort last.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_idle_npus": {"type": "integer", "minimum": 0, "maximum": 64, "default": 1},
                "max_age_seconds": {"type": "integer", "minimum": 0, "maximum": 86400, "default": 300},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "include_disabled": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "list_npu_servers",
        "title": "NPU servers",
        "description": "List monitored servers with cached online and busy-NPU counts.",
        "inputSchema": {"type": "object", "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


def _result(text: str, payload: Any, modern: bool, is_error: bool = False) -> dict[str, Any]:
    result = {"content": [{"type": "text", "text": text}], "structuredContent": payload, "isError": is_error}
    if modern:
        result["resultType"] = "complete"
    return result


def _meta_version(request: dict[str, Any]) -> str | None:
    meta = request.get("params", {}).get("_meta", {}) if isinstance(request.get("params"), dict) else {}
    return meta.get("io.modelcontextprotocol/protocolVersion") if isinstance(meta, dict) else None


def handle_request(request: dict[str, Any], client: VawsTopClient) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    if request_id is None:
        return None
    modern = bool(_meta_version(request)) or method == "server/discover"
    try:
        if method == "server/discover":
            result = {
                "protocolVersions": SUPPORTED_VERSIONS,
                "serverInfo": {"name": "vaws-top", "version": "0.2.0"},
                "capabilities": {"tools": {"listChanged": False}},
            }
        elif method == "initialize":
            requested = request.get("params", {}).get("protocolVersion", LEGACY_VERSION)
            version = requested if requested in SUPPORTED_VERSIONS else LEGACY_VERSION
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "vaws-top", "version": "0.2.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
            if modern:
                result["resultType"] = "complete"
        elif method == "tools/call":
            params = request.get("params") or {}
            arguments = params.get("arguments") or {}
            if params.get("name") == "list_npu_servers":
                payload = client.servers()
                result = _result(format_servers(payload), payload, modern)
            elif params.get("name") == "find_npu_capacity":
                payload = client.capacity(
                    arguments.get("min_idle_npus", 1), arguments.get("max_age_seconds", 300),
                    arguments.get("tags") or [], bool(arguments.get("include_disabled")),
                )
                result = _result(format_capacity(payload), payload, modern)
            elif params.get("name") in ("server_status", "list_mounts"):
                host = arguments.get("host")
                if not isinstance(host, str) or not host.strip():
                    raise ClientError("host is required")
                mode = arguments.get("mode", "cache")
                if mode not in ("cache", "live"):
                    raise ClientError("mode must be cache or live")
                payload = client.server(
                    host, mode, True, bool(arguments.get("process_details")), arguments.get("timeout_seconds", 30),
                )
                text = format_server(payload) if params.get("name") == "server_status" else format_mounts(payload)
                result = _result(text, payload, modern)
            elif params.get("name") == "npu_status":
                host = arguments.get("host")
                if not isinstance(host, str) or not host.strip():
                    raise ClientError("host is required")
                details = bool(arguments.get("process_details"))
                mode = arguments.get("mode", "cache")
                if mode not in ("cache", "live"):
                    raise ClientError("mode must be cache or live")
                payload = client.npu(host, bool(arguments.get("include_processes")) or details, details, mode)
                max_age = arguments.get("max_age_seconds")
                age = payload.get("age_seconds")
                if max_age is not None and (not isinstance(max_age, int) or max_age < 0):
                    raise ClientError("max_age_seconds must be a non-negative integer")
                if max_age is not None and (age is None or age > max_age):
                    result = _result(f"stale snapshot: age={age} max={max_age}\n{format_npu(payload)}", payload, modern, True)
                else:
                    result = _result(format_npu(payload), payload, modern)
            else:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"unknown tool: {params.get('name')}"}}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except ClientError as exc:
        return {"jsonrpc": "2.0", "id": request_id, "result": _result(str(exc), {"error": str(exc)}, modern, True)}
    except Exception as exc:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}


def main() -> int:
    try:
        client = VawsTopClient()
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("message must be an object")
            response = handle_request(request, client)
        except (json.JSONDecodeError, ValueError) as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
