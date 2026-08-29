from __future__ import annotations

import base64
import json
import re
import subprocess
import time
from typing import Any

from .workspace_adapter import WorkspaceDeviceAdapter


SECTION = "__NFM_SECTION__"
EMPLOYEE_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,3}\d{7,9})(?![A-Za-z0-9])")
INITIALS_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,4})(?![A-Za-z0-9])")
INITIALS_STOP_WORDS = {
    "app", "bin", "data", "dev", "etc", "home", "lib", "log", "logs", "mnt",
    "opt", "proc", "root", "run", "src", "sys", "test", "tmp", "usr", "var",
    "vllm", "work",
}
FAST_SCRIPT = r'''set +e
if [ -f /etc/profile.d/vaws-ascend-env.sh ]; then . /etc/profile.d/vaws-ascend-env.sh >/dev/null 2>&1; fi
export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64:${LD_LIBRARY_PATH:-}
printf '__NFM_SECTION__hostname\n'; hostname 2>/dev/null
printf '__NFM_SECTION__proc_stat\n'; head -n 1 /proc/stat 2>/dev/null
printf '__NFM_SECTION__loadavg\n'; cat /proc/loadavg 2>/dev/null
printf '__NFM_SECTION__meminfo\n'; cat /proc/meminfo 2>/dev/null
printf '__NFM_SECTION__uptime\n'; cat /proc/uptime 2>/dev/null
printf '__NFM_SECTION__npu_info\n'; timeout 8 npu-smi info 2>&1; nfm_rc=$?
printf '__NFM_SECTION__npu_info_rc\n%s\n' "$nfm_rc"
printf '__NFM_SECTION__npu_usages\n'; timeout 8 npu-smi info -t usages 2>&1
'''

INFRA_SCRIPT = r'''set +e
printf '__NFM_SECTION__disk\n'; df -P -B1 -x tmpfs -x devtmpfs 2>/dev/null
printf '__NFM_SECTION__mounts\n'; if command -v findmnt >/dev/null 2>&1; then findmnt -J -o TARGET,SOURCE,FSTYPE,OPTIONS 2>/dev/null; else cat /proc/mounts 2>/dev/null; fi
printf '__NFM_SECTION__docker\n'; if command -v docker >/dev/null 2>&1; then docker ps --no-trunc --format '{{json .}}' 2>&1; else printf 'unavailable\n'; fi
printf '__NFM_SECTION__docker_stats\n'; if command -v docker >/dev/null 2>&1; then timeout 10 docker stats --no-stream --format '{{json .}}' 2>/dev/null; fi
printf '__NFM_SECTION__docker_info\n'; if command -v docker >/dev/null 2>&1; then docker info --format '{{json .}}' 2>/dev/null; fi
'''

PROCESS_DETAIL_SCRIPT = r'''set +e
nfm_b64() { printf '%s' "$1" | base64 | tr -d '\n'; }
printf '__NFM_SECTION__process_details\n'
for nfm_pid in __NFM_PIDS__; do
    [ -d "/proc/$nfm_pid" ] || continue
    nfm_user="$(stat -c '%U' "/proc/$nfm_pid" 2>/dev/null)"
    nfm_cwd="$(readlink "/proc/$nfm_pid/cwd" 2>/dev/null)"
    nfm_exe="$(readlink "/proc/$nfm_pid/exe" 2>/dev/null)"
    nfm_name="$(cat "/proc/$nfm_pid/comm" 2>/dev/null)"
    nfm_cmdline="$(tr '\000' ' ' < "/proc/$nfm_pid/cmdline" 2>/dev/null)"
    nfm_cgroup="$(cat "/proc/$nfm_pid/cgroup" 2>/dev/null)"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$nfm_pid" "$(nfm_b64 "$nfm_user")" "$(nfm_b64 "$nfm_cwd")" \
        "$(nfm_b64 "$nfm_cmdline")" "$(nfm_b64 "$nfm_exe")" \
        "$(nfm_b64 "$nfm_name")" "$(nfm_b64 "$nfm_cgroup")"
done
'''


def split_sections(output: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in output.splitlines():
        if line.startswith(SECTION):
            current = line[len(SECTION):].strip()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def parse_meminfo(text: str) -> dict[str, int | None]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"([^:]+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    swap_total = values.get("SwapTotal")
    swap_free = values.get("SwapFree")
    return {
        "memory_total_bytes": total,
        "memory_used_bytes": total - available if total is not None and available is not None else None,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_total - swap_free if swap_total is not None and swap_free is not None else None,
    }


def parse_cpu_stat(text: str) -> tuple[int, int] | None:
    tokens = text.split()
    if not tokens or tokens[0] != "cpu":
        return None
    try:
        values = [int(value) for value in tokens[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def cpu_percent(previous: tuple[int, int] | None, current: tuple[int, int] | None) -> float | None:
    if not previous or not current:
        return None
    total = current[0] - previous[0]
    idle = current[1] - previous[1]
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, (total - idle) * 100.0 / total)), 1)


def parse_disks(text: str) -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    for line in text.splitlines()[1:]:
        cells = line.split()
        if len(cells) < 6:
            continue
        try:
            total, used, available = int(cells[1]), int(cells[2]), int(cells[3])
            percent = float(cells[4].rstrip("%"))
        except ValueError:
            continue
        disks.append({
            "filesystem": cells[0], "total_bytes": total, "used_bytes": used,
            "available_bytes": available, "used_percent": percent, "mount": " ".join(cells[5:]),
        })
    return disks


def parse_mounts(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
        mounts = []

        def append_filesystems(filesystems: list[dict[str, Any]]) -> None:
            for item in filesystems:
                mounts.append({
                    "target": item.get("target"), "source": item.get("source"),
                    "fstype": item.get("fstype"), "options": item.get("options"),
                })
                append_filesystems(item.get("children") or [])

        append_filesystems(payload.get("filesystems", []))
        return mounts
    except (json.JSONDecodeError, AttributeError):
        mounts = []
        for line in text.splitlines():
            cells = line.split()
            if len(cells) >= 4:
                mounts.append({"source": cells[0], "target": cells[1], "fstype": cells[2], "options": cells[3]})
        return mounts


def parse_docker(text: str, stats_text: str = "", info_text: str = "") -> dict[str, Any]:
    if text.strip() == "unavailable":
        return {"available": False, "containers": [], "running": 0}
    containers = []
    error = None
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            error = line[-500:]
            continue
        containers.append({
            "id": item.get("ID"), "name": item.get("Names"), "image": item.get("Image"),
            "status": item.get("Status"), "state": item.get("State"), "ports": item.get("Ports"),
        })
    stats: dict[str, dict[str, Any]] = {}
    for line in stats_text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = str(item.get("Name") or item.get("Container") or "")
        if key:
            stats[key] = {
                "cpu_percent": item.get("CPUPerc"), "memory": item.get("MemUsage"),
                "memory_percent": item.get("MemPerc"), "network_io": item.get("NetIO"),
                "block_io": item.get("BlockIO"), "pids": item.get("PIDs"),
            }
    for container in containers:
        container["stats"] = stats.get(str(container.get("name"))) or stats.get(str(container.get("id")))
    info: dict[str, Any] = {}
    try:
        raw_info = json.loads(info_text)
        info = {
            "server_version": raw_info.get("ServerVersion"), "driver": raw_info.get("Driver"),
            "docker_root_dir": raw_info.get("DockerRootDir"), "containers": raw_info.get("Containers"),
            "containers_running": raw_info.get("ContainersRunning"), "containers_stopped": raw_info.get("ContainersStopped"),
        }
    except (json.JSONDecodeError, AttributeError):
        pass
    return {"available": error is None, "containers": containers, "running": len(containers), "info": info, "error": error}


def build_process_detail_script(pids: list[int]) -> str:
    safe_pids = " ".join(str(pid) for pid in sorted(set(pids)) if pid > 0)
    return PROCESS_DETAIL_SCRIPT.replace("__NFM_PIDS__", safe_pids) + "\nexit 0\n"


def _decode_process_field(value: str) -> str | None:
    if not value:
        return None
    try:
        decoded = base64.b64decode(value).decode("utf-8", errors="replace").strip()
    except (ValueError, TypeError):
        return None
    return decoded or None


def extract_container_id(cgroup: str | None) -> str | None:
    if not cgroup:
        return None
    match = re.search(r"\b([0-9a-f]{64})\b", cgroup, re.IGNORECASE)
    return match.group(1).lower() if match else None


def extract_ownership_labels(cwd: str | None, container_name: str | None) -> list[dict[str, Any]]:
    sources = (("pwd", cwd), ("container", container_name))
    labels: dict[tuple[str, str], dict[str, Any]] = {}
    for kind, pattern in (("employee_id", EMPLOYEE_ID_PATTERN), ("initials", INITIALS_PATTERN)):
        for source, text in sources:
            if not text:
                continue
            for match in pattern.finditer(text):
                value = match.group(1)
                normalized = value.casefold()
                if kind == "initials" and normalized in INITIALS_STOP_WORDS:
                    continue
                key = (kind, normalized)
                label = labels.setdefault(key, {"value": value, "kind": kind, "sources": []})
                if source not in label["sources"]:
                    label["sources"].append(source)
    return list(labels.values())


def parse_process_details(text: str) -> dict[int, dict[str, Any]]:
    details: dict[int, dict[str, Any]] = {}
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) != 7 or not cells[0].isdigit():
            continue
        user, cwd, command, executable, name, cgroup = (_decode_process_field(value) for value in cells[1:])
        pid = int(cells[0])
        details[pid] = {
            "pid": pid,
            "name": name,
            "user": user,
            "cwd": cwd,
            "command": command,
            "executable": executable,
            "container_id": extract_container_id(cgroup),
        }
    return details


def attach_process_details(
    devices: list[dict[str, Any]],
    details: dict[int, dict[str, Any]],
    docker: dict[str, Any],
) -> None:
    containers = docker.get("containers") or []
    by_id = {
        str(container.get("id") or "").lower(): container
        for container in containers
        if container.get("id")
    }
    for device in devices:
        enriched = []
        for record in device.get("processes") or []:
            pid = int(record["pid"])
            detail = details.get(pid) or {}
            container_id = str(detail.get("container_id") or "").lower()
            container = None
            if container_id:
                match = next(
                    (item for item_id, item in by_id.items() if item_id == container_id or item_id.startswith(container_id) or container_id.startswith(item_id)),
                    None,
                )
                container = {
                    "id": (match or {}).get("id") or container_id,
                    "short_id": container_id[:12],
                    "name": (match or {}).get("name"),
                    "image": (match or {}).get("image"),
                    "status": (match or {}).get("status"),
                    "source": "cgroup" if match else "cgroup-unmatched",
                }
            enriched.append({
                **record,
                "name": detail.get("name") or record.get("npu_process_name"),
                "user": detail.get("user"),
                "cwd": detail.get("cwd"),
                "command": detail.get("command"),
                "executable": detail.get("executable"),
                "container": container,
                "ownership_labels": extract_ownership_labels(
                    detail.get("cwd"), (container or {}).get("name"),
                ),
            })
        device["processes"] = enriched


def attach_npu_telemetry(devices: list[dict[str, Any]], info: str) -> None:
    details: dict[int, dict[str, float | str]] = {}
    for line in info.splitlines():
        match = re.search(
            r"\|\s*(\d+)\s+([A-Za-z0-9_.-]+)\s+\|\s*([A-Za-z_.-]+)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)",
            line,
        )
        if match and not match.group(2).isdigit():
            details[int(match.group(1))] = {
                "name": match.group(2), "health": match.group(3),
                "power_w": float(match.group(4)), "temperature_c": float(match.group(5)),
            }
    for device in devices:
        device.update(details.get(int(device["npu_id"]), {}))


def is_device_busy(device: dict[str, Any], hbm_threshold_mb: int) -> bool:
    return (
        bool(device.get("processes"))
        or float(device.get("aicore_percent") or 0) > 1
        or int((device.get("hbm") or {}).get("used_mb") or 0) >= hbm_threshold_mb
    )


class HostProbe:
    def __init__(self, adapter: WorkspaceDeviceAdapter, timeout: int, hbm_busy_threshold_mb: int = 8192) -> None:
        self.adapter = adapter
        self.timeout = timeout
        self.hbm_busy_threshold_mb = hbm_busy_threshold_mb
        self._cpu_counters: dict[str, tuple[int, int]] = {}
        self._infra_cache: dict[str, dict[str, Any]] = {}
        self._process_cache: dict[str, dict[int, dict[str, Any]]] = {}
        self._preflighted: set[str] = set()

    def _collect_process_details(self, server: dict[str, Any], pids: list[int]) -> dict[int, dict[str, Any]]:
        if not pids:
            return {}
        try:
            result = subprocess.run(
                [*self.adapter.ssh_base(server), "bash", "-s"],
                input=build_process_detail_script(pids),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=min(self.timeout, 15), check=False,
                cwd=self.adapter.project_root,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if result.returncode != 0:
            return {}
        return parse_process_details(split_sections(result.stdout).get("process_details", ""))

    def collect(self, server: dict[str, Any], include_infrastructure: bool) -> dict[str, Any]:
        started = time.monotonic()
        if server["id"] not in self._preflighted:
            preflight = self.adapter.preflight(server)
            if not preflight["ok"]:
                raise RuntimeError(str(preflight.get("error") or "本地 OpenSSH 配置预检失败"))
            self._preflighted.add(server["id"])
        # Optional commands (usage details, Docker stats/info) vary across
        # driver/runtime versions. Their non-zero status must not replace the
        # explicit npu_info_rc health gate above.
        script = FAST_SCRIPT + (INFRA_SCRIPT if include_infrastructure else "") + "\nexit 0\n"
        try:
            result = subprocess.run(
                [*self.adapter.ssh_base(server), "bash", "-s"], input=script,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=self.timeout + (12 if include_infrastructure else 0), check=False,
                cwd=self.adapter.project_root,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"SSH 探查超时（{exc.timeout:g}s）") from exc
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "SSH 探查失败")[-1200:])
        sections = split_sections(result.stdout)
        if not sections.get("proc_stat"):
            raise RuntimeError("远程探查结果不完整：缺少 /proc/stat")
        if sections.get("npu_info_rc") not in (None, "0"):
            raise RuntimeError(f"宿主机 npu-smi 探查失败：{sections.get('npu_info', '')[-800:]}")

        server_id = server["id"]
        current_cpu = parse_cpu_stat(sections.get("proc_stat", ""))
        cpu = cpu_percent(self._cpu_counters.get(server_id), current_cpu)
        if current_cpu:
            self._cpu_counters[server_id] = current_cpu
        load_values = sections.get("loadavg", "").split()
        memory = parse_meminfo(sections.get("meminfo", ""))
        npu = self.adapter.parse_npu(sections.get("npu_info", ""), sections.get("npu_usages", ""))
        devices = npu.get("devices", [])
        attach_npu_telemetry(devices, sections.get("npu_info", ""))
        for device in devices:
            device["busy"] = is_device_busy(device, self.hbm_busy_threshold_mb)

        if include_infrastructure:
            infrastructure = {
                "disks": parse_disks(sections.get("disk", "")),
                "mounts": parse_mounts(sections.get("mounts", "")),
                "docker": parse_docker(
                    sections.get("docker", ""), sections.get("docker_stats", ""), sections.get("docker_info", ""),
                ),
            }
            self._infra_cache[server_id] = infrastructure
        else:
            infrastructure = self._infra_cache.get(server_id, {"disks": [], "mounts": [], "docker": {"available": None, "containers": [], "running": None}})

        active_pids = sorted({
            int(process["pid"])
            for device in devices
            for process in (device.get("processes") or [])
            if int(process.get("pid") or 0) > 0
        })
        process_cache = self._process_cache.setdefault(server_id, {})
        stale_pids = active_pids if include_infrastructure else [pid for pid in active_pids if pid not in process_cache]
        process_cache.update(self._collect_process_details(server, stale_pids))
        self._process_cache[server_id] = {pid: process_cache[pid] for pid in active_pids if pid in process_cache}
        attach_process_details(devices, self._process_cache[server_id], infrastructure["docker"])

        hbm_used = sum(int((device.get("hbm") or {}).get("used_mb") or 0) for device in devices)
        hbm_total = sum(int((device.get("hbm") or {}).get("total_mb") or 0) for device in devices)
        utils = [float(device["aicore_percent"]) for device in devices if device.get("aicore_percent") is not None]
        disks = infrastructure["disks"]
        summary = {
            "cpu_percent": cpu, "load1": _float(load_values, 0), "load5": _float(load_values, 1), "load15": _float(load_values, 2),
            **memory,
            "npu_util_percent": round(sum(utils) / len(utils), 1) if utils else None,
            "hbm_used_mb": hbm_used, "hbm_total_mb": hbm_total,
            "npu_count": len(devices), "busy_npu_count": sum(1 for device in devices if device["busy"]),
            "docker_running": infrastructure["docker"].get("running"),
            "disk_max_percent": max((disk["used_percent"] for disk in disks), default=None),
        }
        duration_ms = round((time.monotonic() - started) * 1000)
        return {
            "server_id": server_id, "collected_at": int(time.time()), "duration_ms": duration_ms,
            "hostname": sections.get("hostname") or server["name"], "summary": summary,
            "devices": devices, **infrastructure,
        }


def _float(values: list[str], index: int) -> float | None:
    try:
        return float(values[index])
    except (IndexError, ValueError):
        return None
