from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


class RemoteError(RuntimeError):
    """Remote bootstrap or runtime materialization failure."""


@dataclass(frozen=True)
class CredentialGroup:
    mode: str
    username: str
    password: Optional[str] = None
    password_env: Optional[str] = None
    key_path: Optional[str] = None
    token_env: Optional[str] = None
    simulation_root: Optional[Path] = None

    @property
    def resolved_password(self) -> Optional[str]:
        if self.password:
            return self.password
        if self.password_env:
            value = os.environ.get(self.password_env)
            if value:
                return value
        return None


@dataclass(frozen=True)
class HostSpec:
    name: str
    host: str
    port: int
    login_user: str
    auth_group: str
    ssh_auth_ref: Optional[str] = None


@dataclass(frozen=True)
class RuntimeSpec:
    image_ref: str
    container_name: str
    ssh_port: int
    workspace_root: str
    bootstrap_mode: str
    host_workspace_path: str
    docker_run_args: List[str]


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    status: str
    detail: Optional[str] = None

    def to_mapping(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"name": self.name, "status": self.status}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class VerificationResult:
    status: str
    summary: str
    checks: List[VerificationCheck]
    runtime: Dict[str, Any]

    @classmethod
    def ready(
        cls,
        *,
        summary: str,
        runtime: Dict[str, Any],
        checks: Optional[List[VerificationCheck]] = None,
    ) -> "VerificationResult":
        return cls(
            status="ready",
            summary=summary,
            checks=list(checks or []),
            runtime=dict(runtime),
        )

    @classmethod
    def needs_repair(
        cls,
        *,
        summary: str,
        runtime: Dict[str, Any],
        checks: List[VerificationCheck],
    ) -> "VerificationResult":
        return cls(
            status="needs_repair",
            summary=summary,
            checks=list(checks),
            runtime=dict(runtime),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "checks": [check.to_mapping() for check in self.checks],
            "runtime": dict(self.runtime),
        }


@dataclass(frozen=True)
class CleanupResult:
    server_name: str
    status: str
    detail: str

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "server_name": self.server_name,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TargetContext:
    name: str
    host: HostSpec
    credential: CredentialGroup
    runtime: RuntimeSpec
