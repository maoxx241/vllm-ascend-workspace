#!/usr/bin/env python3
"""Conservative local GC for stale VAWS session metadata.

Metadata loss and an unreachable container never prove resources are free.
``--reap-dead`` checks the host Docker state and repeatedly observes leased
devices free before releasing leases. All uncertainty retains ownership.
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
from vaws_session_state import load_index, load_leases, load_session_lookup, release_all_session_leases, session_live_leases  # noqa: E402

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
    # sshd can fail while container workers remain alive. This includes
    # connection refused, authentication failure, routing failure and timeout.
    return {
        "alive": None,
        "reason": f"container ssh unreachable; workload state unknown (rc={result.returncode})",
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
        help="apply releases proven safe by --reap-dead; metadata alone never releases leases",
    )
    parser.add_argument(
        "--reap-dead",
        action="store_true",
        help=(
            "verify container absence/stopped state on the host and repeatedly "
            "confirm leased NPUs free before reaping; inconclusive probes retain leases"
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
                active.append(sid)
                continue

            # Non-removed session. Optionally probe container liveness so a
            # zombie (ready state but dead container) can release its leases.
            if args.reap_dead and sid in lease_owners:
                live = session_live_leases(repo_root=lookup.state_repo_root, machine_alias=session["base_machine"], session_id=sid)
                probe = _probe_session_container(session, devices=live["npu_devices"])
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


def _probe_session_container(session: dict[str, Any], *, devices: list[int] | None = None) -> dict[str, Any]:
    try:
        remote = session["remote"]
        host = remote["host"]
        port = int(remote.get("host_port", 22))
        user = remote.get("host_user", "root")
        name = remote["container"]["name"]
    except (KeyError, TypeError, ValueError) as exc:
        return {"alive": None, "reason": f"session missing container endpoint: {exc}"}
    # Reuse the coordinator's NPU parser/confirmation logic on the host. Docker
    # and NPU commands use argv, never interpolated shell expressions.
    source = (LIB_DIR / "vaws_npu_coordination.py").read_text(encoding="utf-8")
    runner = r'''
import sys
request = json.loads(sys.argv[1])
names = subprocess.run(["docker", "container", "ls", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=10)
if names.returncode:
    raise RuntimeError("host Docker state unavailable")
exists = request["name"] in names.stdout.splitlines()
running = False
if exists:
    state = subprocess.run(["docker", "inspect", "--type", "container", request["name"]], capture_output=True, text=True, timeout=10, check=True)
    running = bool(json.loads(state.stdout)[0]["State"]["Running"])
if running:
    result = {"alive": True, "reason": "host confirms container is running"}
else:
    wanted = set(request["devices"])
    observed = _confirmed_free_probe(samples=2, interval_seconds=1, probe=probe_npu_occupancy) if wanted else {"status": "ok", "free": []}
    free = observed.get("status") == "ok" and wanted.issubset(set(observed.get("free", [])))
    result = {"alive": False if free else None, "reason": "host confirms stopped/absent container and free devices" if free else "device occupancy is busy or unknown", "container_exists": exists}
print(json.dumps(result))
'''
    request = {"name": name, "devices": devices if devices is not None else session.get("leases", {}).get("npu_devices", [])}
    command = shlex.join(["python3", "-c", source + "\n" + runner, json.dumps(request)])
    try:
        result = subprocess.run(
            ["ssh", *base_ssh_options(), "-o", f"ConnectTimeout={REAP_SSH_TIMEOUT_SECONDS}", "-p", str(port), f"{user}@{host}", command],
            capture_output=True, text=True, check=False, timeout=60,
        )
        if result.returncode:
            return {"alive": None, "reason": "host confirmation failed", "returncode": result.returncode}
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("alive") not in (True, False, None):
            raise ValueError("invalid host confirmation response")
        return payload
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"alive": None, "reason": f"host confirmation inconclusive: {exc}"}


if __name__ == "__main__":
    raise SystemExit(main())
