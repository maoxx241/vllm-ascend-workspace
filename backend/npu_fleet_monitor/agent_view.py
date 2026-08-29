from __future__ import annotations

import time
from typing import Any


class AgentQueryError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _percent(used: int | float | None, total: int | float | None) -> float | None:
    if not total:
        return None
    return round(float(used or 0) * 100 / float(total), 1)


def _process(process: dict[str, Any], detailed: bool) -> dict[str, Any]:
    container = process.get("container") or {}
    result = {
        "pid": process.get("pid"),
        "name": process.get("name") or process.get("npu_process_name"),
        "npu_memory_mb": process.get("npu_memory_mb"),
        "container": container.get("name") or container.get("short_id"),
        "owners": [label.get("value") for label in process.get("ownership_labels") or [] if label.get("value")],
    }
    if detailed:
        result.update({
            "user": process.get("user"),
            "cwd": process.get("cwd"),
            "command": process.get("command"),
            "executable": process.get("executable"),
        })
    return {key: value for key, value in result.items() if value not in (None, [], "")}


def compact_server(server: dict[str, Any], snapshot: dict[str, Any] | None, now: int | None = None) -> dict[str, Any]:
    current_time = int(time.time()) if now is None else now
    status = snapshot.get("status") if snapshot else ("offline" if server.get("last_error") else "pending")
    summary = (snapshot or {}).get("summary") or {}
    collected_at = (snapshot or {}).get("collected_at")
    return {
        "id": server["id"],
        "name": server["name"],
        "host": server["host"],
        "enabled": bool(server.get("enabled")),
        "tags": server.get("tags") or [],
        "status": status,
        "age_seconds": max(0, current_time - int(collected_at)) if collected_at else None,
        "npu_count": summary.get("npu_count") or 0,
        "busy_npu_count": summary.get("busy_npu_count") or 0,
    }


def find_server(
    query: str,
    servers: list[dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    value = query.strip().casefold()
    if not value or len(value) > 255:
        raise AgentQueryError("host must be 1-255 characters")
    matches = []
    for server in servers:
        snapshot = snapshots.get(server["id"])
        candidates = {str(server.get(key) or "").casefold() for key in ("id", "name", "host")}
        candidates.add(str((snapshot or {}).get("hostname") or "").casefold())
        if value in candidates:
            matches.append((server, snapshot))
    if not matches:
        raise AgentQueryError(f"server not found: {query}", 404)
    if len(matches) > 1:
        hosts = ", ".join(f"{server['host']}:{server['port']}" for server, _ in matches)
        raise AgentQueryError(f"server is ambiguous: {hosts}", 409)
    return matches[0]


def npu_status(
    server: dict[str, Any],
    snapshot: dict[str, Any] | None,
    *,
    include_processes: bool = False,
    detailed_processes: bool = False,
    now: int | None = None,
) -> dict[str, Any]:
    current_time = int(time.time()) if now is None else now
    compact = compact_server(server, snapshot, current_time)
    summary = (snapshot or {}).get("summary") or {}
    collected_at = (snapshot or {}).get("collected_at")
    devices = []
    for device in (snapshot or {}).get("devices") or []:
        hbm = device.get("hbm") or {}
        processes = device.get("processes") or []
        owners = []
        containers = []
        for process in processes:
            container = process.get("container") or {}
            if container.get("name") and container["name"] not in containers:
                containers.append(container["name"])
            for label in process.get("ownership_labels") or []:
                if label.get("value") and label["value"] not in owners:
                    owners.append(label["value"])
        row = {
            "id": device.get("npu_id"),
            "name": device.get("name"),
            "aicore_percent": device.get("aicore_percent"),
            "hbm_used_mb": hbm.get("used_mb"),
            "hbm_total_mb": hbm.get("total_mb"),
            "hbm_percent": _percent(hbm.get("used_mb"), hbm.get("total_mb")),
            "busy": bool(device.get("busy")),
            "process_count": len(processes),
            "containers": containers,
            "owners": owners,
        }
        if include_processes:
            row["processes"] = [_process(process, detailed_processes) for process in processes]
        devices.append(row)
    return {
        "source": "cache",
        "server": {
            "id": compact["id"], "name": compact["name"], "host": compact["host"],
            "enabled": compact["enabled"], "tags": compact["tags"], "status": compact["status"],
        },
        "collected_at": collected_at,
        "age_seconds": compact["age_seconds"],
        "summary": {
            "npu_count": summary.get("npu_count") or 0,
            "busy_npu_count": summary.get("busy_npu_count") or 0,
            "idle_npu_count": max(0, (summary.get("npu_count") or 0) - (summary.get("busy_npu_count") or 0)),
            "aicore_percent": summary.get("npu_util_percent"),
            "hbm_used_mb": summary.get("hbm_used_mb") or 0,
            "hbm_total_mb": summary.get("hbm_total_mb") or 0,
        },
        "devices": devices,
        **({"error": str((snapshot or {}).get("error") or server.get("last_error"))[-240:]} if compact["status"] == "offline" else {}),
    }


def server_status(
    server: dict[str, Any],
    snapshot: dict[str, Any] | None,
    *,
    include_processes: bool = True,
    detailed_processes: bool = False,
    include_infrastructure: bool = True,
    now: int | None = None,
) -> dict[str, Any]:
    result = npu_status(
        server, snapshot, include_processes=include_processes,
        detailed_processes=detailed_processes, now=now,
    )
    summary = (snapshot or {}).get("summary") or {}
    memory_total = summary.get("memory_total_bytes") or 0
    memory_used = summary.get("memory_used_bytes") or 0
    result["system"] = {
        "cpu_percent": summary.get("cpu_percent"),
        "load": [summary.get("load1"), summary.get("load5"), summary.get("load15")],
        "memory_used_bytes": memory_used,
        "memory_total_bytes": memory_total,
        "memory_percent": _percent(memory_used, memory_total),
        "disk_max_percent": summary.get("disk_max_percent"),
        "docker_running": summary.get("docker_running"),
    }
    if include_infrastructure:
        disks = (snapshot or {}).get("disks") or []
        by_mount = {disk.get("mount"): disk for disk in disks}
        mounts = []
        for mount in (snapshot or {}).get("mounts") or []:
            target = mount.get("target")
            disk = by_mount.get(target) or {}
            target_text = str(target or "").casefold()
            fstype = str(mount.get("fstype") or "").casefold()
            mounts.append({
                "target": target,
                "source": mount.get("source"),
                "fstype": mount.get("fstype"),
                "options": mount.get("options"),
                "total_bytes": disk.get("total_bytes"),
                "available_bytes": disk.get("available_bytes"),
                "used_percent": disk.get("used_percent"),
                "weight_candidate": (
                    any(token in target_text for token in ("model", "weight", "/data", "/mnt", "shared"))
                    or fstype in ("nfs", "nfs4", "cifs", "ceph", "lustre")
                ),
            })
        result["storage"] = {"disks": disks, "mounts": mounts}
        result["docker"] = (snapshot or {}).get("docker") or {"available": None, "containers": []}
    return result


def capacity_candidates(
    servers: list[dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
    *,
    min_idle_npus: int = 1,
    max_age_seconds: int = 300,
    tags: list[str] | None = None,
    include_disabled: bool = False,
    now: int | None = None,
) -> dict[str, Any]:
    current_time = int(time.time()) if now is None else now
    required_tags = {tag.casefold() for tag in tags or []}
    candidates = []
    for server in servers:
        if not include_disabled and not server.get("enabled"):
            continue
        if required_tags and not required_tags.issubset({str(tag).casefold() for tag in server.get("tags") or []}):
            continue
        snapshot = snapshots.get(server["id"])
        compact = compact_server(server, snapshot, current_time)
        summary = (snapshot or {}).get("summary") or {}
        idle = max(0, compact["npu_count"] - compact["busy_npu_count"])
        if compact["status"] != "online" or compact["age_seconds"] is None or compact["age_seconds"] > max_age_seconds:
            continue
        if idle < min_idle_npus:
            continue
        candidates.append({
            **compact,
            "idle_npu_count": idle,
            "aicore_percent": summary.get("npu_util_percent"),
            "hbm_used_mb": summary.get("hbm_used_mb") or 0,
            "hbm_total_mb": summary.get("hbm_total_mb") or 0,
            "cpu_percent": summary.get("cpu_percent"),
            "memory_percent": _percent(summary.get("memory_used_bytes"), summary.get("memory_total_bytes")),
            "disk_max_percent": summary.get("disk_max_percent"),
        })
    candidates.sort(key=lambda item: (
        "低优先级" in item["tags"], -item["idle_npu_count"], item["age_seconds"], item["host"],
    ))
    return {
        "source": "cache", "requirements": {
            "min_idle_npus": min_idle_npus, "max_age_seconds": max_age_seconds, "tags": tags or [],
        },
        "candidates": candidates,
    }
