#!/usr/bin/env python3
"""Business helpers for vllm-ascend-serving: presets, progress, health probes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_remote_dev import ssh_exec as remote_ssh_exec  # noqa: E402
from vaws_remote_target import SshEndpoint  # noqa: E402
from vaws_result_envelope import emit_skill_json, progress as envelope_progress  # noqa: E402
from vaws_validate import parse_device_csv  # noqa: E402

SSH_CONNECT_TIMEOUT_SECONDS = 15
SSH_EXEC_DEFAULT_TIMEOUT_SECONDS = 180
PRESETS_DIR = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "presets"
SERVICE_NAME = "vllm"


def ssh_exec(endpoint: SshEndpoint, script: str, *, check: bool = True, timeout: float | None = SSH_EXEC_DEFAULT_TIMEOUT_SECONDS):
    return remote_ssh_exec(
        endpoint,
        script,
        check=check,
        timeout=timeout,
        connect_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
    )


def load_preset(name: str) -> dict[str, Any]:
    stem = name[:-5] if name.endswith(".json") else name
    if not stem or "/" in stem or "\\" in stem or ".." in stem:
        raise ValueError(f"invalid preset name {name!r}: use a bare preset name")
    path = PRESETS_DIR / f"{stem}.json"
    if not path.is_file():
        available = sorted(p.stem for p in PRESETS_DIR.glob("*.json")) if PRESETS_DIR.is_dir() else []
        raise ValueError(
            f"unknown preset {name!r}; available presets: "
            + (", ".join(available) if available else "(none)")
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"preset {name!r} must contain a JSON object")
    return data


def emit_progress(phase: str, message: str, **extra: Any) -> None:
    envelope_progress(phase, message, **extra)


def print_json(data: dict[str, Any]) -> None:
    emit_skill_json(
        data,
        skill="vllm-ascend-serving",
        entry_point=".agents/skills/vllm-ascend-serving/scripts/serve_status.py",
    )


def now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_devices_csv(value: str) -> list[int]:
    if not value or not str(value).strip():
        return []
    return list(parse_device_csv(str(value)) or [])


def endpoint_from_reply(reply: dict[str, Any]) -> SshEndpoint:
    target = reply.get("target") if isinstance(reply.get("target"), dict) else {}
    endpoint = target.get("endpoint") if isinstance(target.get("endpoint"), dict) else reply.get("endpoint")
    if not isinstance(endpoint, dict) or not endpoint.get("host"):
        raise RuntimeError("coordinator reply has no ordinary endpoint")
    return SshEndpoint(
        host=str(endpoint["host"]),
        port=int(endpoint.get("port") or 22),
        user=str(endpoint.get("user") or "root"),
    )


def service_port_of(reply: dict[str, Any]) -> int | None:
    port = reply.get("service_port")
    if port in (None, ""):
        target = reply.get("target") if isinstance(reply.get("target"), dict) else {}
        port = target.get("service_port")
        env = target.get("environment") if isinstance(target.get("environment"), dict) else {}
        if port in (None, "") and env.get("VAWS_SERVICE_PORT"):
            port = env["VAWS_SERVICE_PORT"]
    if port in (None, ""):
        return None
    return int(port)
