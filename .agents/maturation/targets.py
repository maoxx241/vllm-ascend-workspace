"""Resolve endpoints under test and give them anonymous, stable labels.

Identities come from the untracked machine inventory or explicit
``host:port[:kind]`` arguments. Labels (``host-a``, ``host-b-ctr`` …) are
what every report and candidate uses; the label→identity map is written only
to the untracked evidence directory.
"""

from __future__ import annotations

import string
from typing import Any, Iterable, Mapping

from .spec import ENDPOINT_KINDS


class TargetError(ValueError):
    """Raised for unusable endpoint declarations."""


def parse_endpoint_arg(value: str) -> dict[str, Any]:
    parts = value.split(":")
    if len(parts) not in {2, 3} or not parts[0] or not parts[1].isdigit():
        raise TargetError(f"endpoint must look like HOST:PORT[:KIND], got {value!r}")
    kind = parts[2] if len(parts) == 3 else "host"
    if kind not in ENDPOINT_KINDS:
        raise TargetError(f"endpoint kind must be one of {sorted(ENDPOINT_KINDS)}, got {kind!r}")
    return {"host": parts[0], "port": int(parts[1]), "user": "root", "kind": kind, "alias": parts[0]}


def endpoints_from_inventory(
    document: Mapping[str, Any],
    *,
    include_containers: bool = False,
    select: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    machines = document.get("machines", [])
    if isinstance(machines, Mapping):
        machines = [{"alias": alias, **record} for alias, record in machines.items() if isinstance(record, Mapping)]
    if not isinstance(machines, list):
        raise TargetError("inventory machines must be a list or mapping")
    wanted = set(select or [])
    endpoints: list[dict[str, Any]] = []
    for record in machines:
        if not isinstance(record, Mapping):
            continue
        alias = str(record.get("alias") or "")
        host_block = record.get("host") if isinstance(record.get("host"), Mapping) else {}
        host = str(host_block.get("ip") or host_block.get("host") or alias)
        if not host:
            continue
        if wanted and alias not in wanted and host not in wanted:
            continue
        endpoints.append(
            {
                "host": host,
                "port": int(host_block.get("port") or 22),
                "user": str(host_block.get("user") or "root"),
                "kind": "host",
                "alias": alias or host,
            }
        )
        container = record.get("container") if isinstance(record.get("container"), Mapping) else {}
        if include_containers and container.get("ssh_port"):
            endpoints.append(
                {
                    "host": host,
                    "port": int(container["ssh_port"]),
                    "user": str(container.get("user") or "root"),
                    "kind": "container",
                    "alias": alias or host,
                }
            )
    return endpoints


def assign_labels(endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic labels: one letter per distinct host, ``-ctr`` for containers."""
    hosts = sorted({str(item["host"]) for item in endpoints})
    letters: dict[str, str] = {}
    for index, host in enumerate(hosts):
        letters[host] = _letter(index)
    labelled: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sorted(endpoints, key=lambda e: (str(e["host"]), str(e["kind"]) != "host", int(e["port"]))):
        base = f"host-{letters[str(item['host'])]}"
        label = base if item["kind"] == "host" else f"{base}-ctr"
        suffix = 2
        while label in seen:
            label = f"{base}-{item['kind']}{suffix}"
            suffix += 1
        seen.add(label)
        labelled.append({**item, "label": label})
    return labelled


def _letter(index: int) -> str:
    alphabet = string.ascii_lowercase
    label = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        label = alphabet[remainder] + label
    return label
