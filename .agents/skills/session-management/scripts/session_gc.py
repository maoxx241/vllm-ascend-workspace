#!/usr/bin/env python3
"""Conservative local GC for stale VAWS session metadata.

By default only sessions whose metadata is ``removed`` or unreadable release
their leases. ``--reap-dead`` additionally probes the container SSH endpoint of
non-removed lease holders and reaps the ones whose container is *confirmed*
gone (connection refused, not a timeout). This closes the "zombie session"
gap: a session left ``ready`` in local state after its container died holds
NPU/port leases indefinitely and blocks the whole machine (observed with
``dsv4-w4a8-main-125`` holding all 8 cards for days).
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_ssh import base_ssh_options  # noqa: E402
from vaws_session_state import load_index, load_leases, load_session_lookup, release_all_session_leases  # noqa: E402

REAP_SSH_TIMEOUT_SECONDS = 20


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def probe_container_alive(host: str, port: int, user: str = "root") -> dict[str, Any]:
    """Probe a session container's SSH endpoint.

    Returns a verdict with ``alive`` True/False/None. ``None`` means the probe
    was inconclusive (timeout / transient), so the caller must NOT reap.
    """
    cmd = [
        "ssh",
        *base_ssh_options(),
        "-o",
        f"ConnectTimeout={REAP_SSH_TIMEOUT_SECONDS}",
        "-p",
        str(port),
        f"{user}@{host}",
        "true",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=REAP_SSH_TIMEOUT_SECONDS + 10,
        )
    except subprocess.TimeoutExpired:
        return {"alive": None, "reason": "ssh probe timed out (inconclusive)"}
    if result.returncode == 0:
        return {"alive": True, "reason": "container ssh reachable"}
    # Connection refused / no route / auth failures on a dead container come
    # back quickly with a non-zero rc. Treat as confirmed dead.
    return {
        "alive": False,
        "reason": f"container ssh unreachable (rc={result.returncode})",
        "stderr_tail": (result.stderr or "")[-200:],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="show stale lease releases without mutating state (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="actually release leases for removed or missing session metadata",
    )
    parser.add_argument(
        "--reap-dead",
        action="store_true",
        help=(
            "also probe container SSH for non-removed lease holders and reap "
            "leases whose container is confirmed dead (connection refused). "
            "Inconclusive probes (timeouts) are never reaped."
        ),
    )
    return parser


def lease_owner_session_ids(leases: dict[str, Any]) -> set[str]:
    owners: set[str] = set()
    for bucket in leases.get("leases", {}).values():
        if not isinstance(bucket, dict):
            continue
        for kind in ("npu_devices", "container_ssh_ports", "service_ports"):
            records = bucket.get(kind, {})
            if not isinstance(records, dict):
                continue
            for record in records.values():
                if isinstance(record, dict) and isinstance(record.get("session_id"), str):
                    owners.add(record["session_id"])
    return owners


def main() -> int:
    args = build_parser().parse_args()
    dry_run = not args.apply
    try:
        index = load_index(ROOT)
        leases = load_leases(ROOT)
        lease_owners = lease_owner_session_ids(leases)
        released: list[str] = []
        checked: list[dict[str, Any]] = []
        active: list[str] = []
        reaped_dead: list[str] = []
        candidates = set(index.get("sessions", {})) | lease_owners
        for sid in sorted(candidates):
            try:
                lookup = load_session_lookup(session_id=sid, repo_root=ROOT)
                session = lookup.session
            except Exception as exc:  # noqa: BLE001
                state = "orphan-lease" if sid not in index.get("sessions", {}) else "missing-state"
                checked.append({"session_id": sid, "status": state, "error": str(exc)})
                if not dry_run:
                    release_all_session_leases(repo_root=ROOT, session_id=sid)
                released.append(sid)
                continue
            if session.get("status") == "removed":
                if not dry_run:
                    release_all_session_leases(repo_root=lookup.state_repo_root, session_id=sid)
                released.append(sid)
                checked.append({"session_id": sid, "status": session.get("status")})
                continue

            # Non-removed session. Optionally probe container liveness so a
            # zombie (ready state but dead container) can release its leases.
            if args.reap_dead and sid in lease_owners:
                probe = _probe_session_container(session)
                entry: dict[str, Any] = {
                    "session_id": sid,
                    "status": session.get("status"),
                    "container_probe": probe,
                }
                if probe.get("alive") is False:
                    if not dry_run:
                        release_all_session_leases(repo_root=lookup.state_repo_root, session_id=sid)
                    released.append(sid)
                    reaped_dead.append(sid)
                    entry["reaped"] = True
                else:
                    active.append(sid)
                checked.append(entry)
            else:
                active.append(sid)
                checked.append({"session_id": sid, "status": session.get("status")})
        print_json(
            {
                "status": "ok",
                "dry_run": dry_run,
                "reap_dead": args.reap_dead,
                "checked": checked,
                "active_session_leases": sorted(set(active) & lease_owners),
                "released_lease_sessions": [] if dry_run else sorted(set(released)),
                "would_release_lease_sessions": sorted(set(released)) if dry_run else [],
                "reaped_dead_containers": sorted(set(reaped_dead)),
            }
        )
        return 0
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2


def _probe_session_container(session: dict[str, Any]) -> dict[str, Any]:
    try:
        remote = session["remote"]
        host = remote["host"]
        port = int(remote["container"]["ssh_port"])
    except (KeyError, TypeError, ValueError) as exc:
        return {"alive": None, "reason": f"session missing container endpoint: {exc}"}
    return probe_container_alive(host, port)


if __name__ == "__main__":
    raise SystemExit(main())
