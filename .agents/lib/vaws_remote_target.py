"""Ordinary host/port endpoints. Not a VAWS identity resolver."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


OPTIONAL_ASCEND_ENV_FILE = "/etc/profile.d/vaws-ascend-env.sh"


class RemoteTargetError(RuntimeError):
    """Deterministic user-facing endpoint failure."""


@dataclass(frozen=True)
class SshEndpoint:
    host: str
    port: int
    user: str = "root"

    def destination(self) -> str:
        return f"{self.user}@{self.host}"

    def known_hosts_key(self) -> str:
        return self.host if self.port == 22 else f"[{self.host}]:{self.port}"

    def to_dict(self, *, plane: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "destination": self.destination(),
            "known_hosts_key": self.known_hosts_key(),
        }
        if plane:
            payload["plane"] = plane
        return payload


def ssh_endpoint_from_mapping(data: dict[str, Any] | None) -> SshEndpoint:
    if not isinstance(data, dict) or not data.get("host"):
        raise RemoteTargetError("endpoint mapping is missing host")
    return SshEndpoint(
        host=str(data["host"]),
        port=int(data.get("port") or 22),
        user=str(data.get("user") or "root"),
    )


def json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)


def print_json(data: dict[str, Any]) -> None:
    print(json_dumps(data))


def ascend_env_preamble(*, set_e: bool = True, export_driver_lib: bool = False) -> str:
    """Optional remote snippet. Coordinator launch env is authoritative."""
    lines: list[str] = []
    if set_e:
        lines.append("set -e")
    lines.extend(
        [
            f"if [ -f {OPTIONAL_ASCEND_ENV_FILE} ]; then",
            "  set +u",
            f"  source {OPTIONAL_ASCEND_ENV_FILE}",
            "  set -u",
            "fi",
        ]
    )
    if export_driver_lib:
        lines.append(
            "export LD_LIBRARY_PATH="
            '"/usr/local/Ascend/driver/lib64/driver'
            ":/usr/local/Ascend/driver/lib64"
            '${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"'
        )
    return "\n".join(lines)
