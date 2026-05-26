from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import EndpointError

DEFAULT_USER = os.environ.get("REMOTE_DEV_DEFAULT_USER", "root")
DEFAULT_ROOT = os.environ.get("REMOTE_DEV_DEFAULT_ROOT", "/")
DEFAULT_CWD = os.environ.get("REMOTE_DEV_DEFAULT_CWD", "/vllm-workspace")


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    user: str = DEFAULT_USER
    root: str = DEFAULT_ROOT
    cwd: str | None = None
    runtime_env: bool = True
    identity_file: str | None = None
    connect_timeout_ms: int = 10000
    kind: str = "direct-endpoint"
    alias: str | None = None
    source: dict[str, Any] | None = None

    @property
    def effective_cwd(self) -> str:
        return self.cwd or DEFAULT_CWD or self.root

    @property
    def endpoint_key(self) -> str:
        return f"{self.user}@{self.host}:{self.port}|root={self.root}"

    @property
    def endpoint_id(self) -> str:
        return hashlib.sha256(self.endpoint_key.encode("utf-8")).hexdigest()[:16]

    def destination(self) -> str:
        return f"{self.user}@{self.host}"

    def to_result_target(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "endpoint_id": self.endpoint_id,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "root": self.root,
            "cwd": self.effective_cwd,
            "runtime_env": self.runtime_env,
        }
        if self.alias:
            payload["alias"] = self.alias
        if self.source:
            payload["source"] = self.source
        return payload


def substrate_root() -> Path:
    return Path(__file__).resolve().parents[1]


def repo_root() -> Path:
    return substrate_root().parent


def _read_endpoint_aliases() -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in (substrate_root() / "endpoints.json", substrate_root() / "endpoints.local.json"):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise EndpointError(f"invalid endpoint alias file {path}: {exc}") from exc
        entries = data.get("endpoints", data if isinstance(data, dict) else {})
        if not isinstance(entries, dict):
            raise EndpointError(f"endpoint alias file {path} must be an object")
        merged.update(entries)
    return merged


def _direct_endpoint(payload: dict[str, Any]) -> Endpoint:
    if "host" not in payload or "port" not in payload:
        raise EndpointError("direct endpoint requires host and port")
    try:
        port = int(payload["port"])
    except (TypeError, ValueError) as exc:
        raise EndpointError("endpoint port must be an integer") from exc
    return Endpoint(
        host=str(payload["host"]),
        port=port,
        user=str(payload.get("user") or DEFAULT_USER),
        root=str(payload.get("root") or DEFAULT_ROOT),
        cwd=str(payload["cwd"]) if payload.get("cwd") else None,
        runtime_env=bool(payload.get("runtime_env", True)),
        identity_file=str(payload["identity_file"]) if payload.get("identity_file") else None,
        connect_timeout_ms=int(payload.get("connect_timeout_ms") or 10000),
        kind=str(payload.get("kind") or "direct-endpoint"),
        alias=str(payload["alias"]) if payload.get("alias") else None,
        source=payload.get("source") if isinstance(payload.get("source"), dict) else None,
    )


def _endpoint_from_managed(payload: dict[str, Any]) -> Endpoint:
    lib_dir = repo_root() / ".agents" / "lib"
    if not lib_dir.exists():
        raise EndpointError("managed machine/session resolution requires .agents/lib")
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    try:
        from vaws_remote_toolbox import resolve_remote_target  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise EndpointError(f"failed to import VAWS target resolver: {exc}") from exc
    try:
        target = resolve_remote_target(
            machine=payload.get("machine"),
            session_id=payload.get("session_id"),
            session_file=payload.get("session_file"),
            repo_root=repo_root(),
        )
    except Exception as exc:  # noqa: BLE001
        raise EndpointError(f"failed to resolve managed target: {exc}") from exc
    endpoint = target.container_endpoint
    return Endpoint(
        host=endpoint.host,
        port=int(endpoint.port),
        user=endpoint.user,
        root=str(payload.get("root") or DEFAULT_ROOT),
        cwd=str(payload.get("cwd") or target.runtime_root),
        runtime_env=bool(payload.get("runtime_env", True)),
        identity_file=str(payload["identity_file"]) if payload.get("identity_file") else None,
        connect_timeout_ms=int(payload.get("connect_timeout_ms") or 10000),
        kind="managed-session" if payload.get("session_id") or payload.get("session_file") else "managed-machine",
        alias=str(payload.get("session_id") or payload.get("machine") or target.alias),
        source={"vaws_target": target.to_dict()},
    )


def resolve_endpoint(payload: dict[str, Any]) -> Endpoint:
    if payload.get("host") and payload.get("port"):
        return _direct_endpoint(payload)
    if payload.get("alias"):
        aliases = _read_endpoint_aliases()
        alias = str(payload["alias"])
        if alias not in aliases:
            raise EndpointError(f"endpoint alias {alias!r} is not configured")
        merged = {**aliases[alias], **payload}
        merged["alias"] = alias
        return _direct_endpoint(merged)
    if payload.get("session_id") or payload.get("session_file") or payload.get("machine"):
        return _endpoint_from_managed(payload)
    raise EndpointError("provide host+port, alias, session_id/session_file, or machine")
