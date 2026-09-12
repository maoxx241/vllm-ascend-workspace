"""Ordinary host/port endpoints. Not a VAWS identity resolver."""
from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

from remote_dev.core.endpoint import Endpoint as SshEndpoint


OPTIONAL_ASCEND_ENV_FILE = "/etc/profile.d/vaws-ascend-env.sh"


class RemoteTargetError(RuntimeError):
    """Deterministic user-facing endpoint failure."""


def ssh_endpoint_from_mapping(data: dict[str, Any] | None) -> SshEndpoint:
    if not isinstance(data, dict) or not data.get("host"):
        raise RemoteTargetError("endpoint mapping is missing host")
    values = {field.name: data[field.name] for field in fields(SshEndpoint) if field.name in data}
    values.setdefault("port", 22)
    return SshEndpoint(**values)


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
