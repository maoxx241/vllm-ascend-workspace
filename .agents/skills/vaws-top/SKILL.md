---
name: vaws-top
description: Query cached or live NPU fleet capacity through the local vaws-top CLI/MCP, including NPU/HBM, CPU, memory, Docker containers, process ownership, and mounts. Use when an agent needs to choose or inspect a monitored NPU server. Do not use to launch workloads, reserve NPUs, or manage remote containers.
---

# vaws-top

Use the running local monitor as the single server-status interface. Run commands from the vaws-top project root that contains this Skill.

## Boundaries

- Prefer vaws-top over ad hoc SSH for fleet status. Its service owns remote probing, credentials, connection reuse, and snapshot collection.
- Fleet discovery, `capacity`, `npu`, and `mounts` default to cache and never trigger SSH.
- CLI `status` defaults to live: it requests one centralized probe and waits for a new snapshot. Add `--cache` when a cached full-server view is sufficient.
- The web/API service remains on `127.0.0.1` and has no login. Do not expose it through a public listener, reverse proxy, or port forward.
- Capacity results are observations, not reservations. Use the owning session or workload workflow before starting an experiment.
- Do not print or inspect `data/keys`, passwords, or `known_hosts` contents.

## Server-selection flow

1. Shortlist fresh cached capacity:

   ```bash
   python3 scripts/vaws-top.py capacity --min-idle 4 --max-age 180 --tag A3
   ```

2. Inspect a candidate from cache:

   ```bash
   python3 scripts/vaws-top.py status 10.18.4.21 --cache
   ```

   The compact result includes NPU/HBM, CPU, memory, disk pressure, Docker count, grouped NPU processes, containers, extracted employee IDs or initials, and likely model-weight mounts.

3. Inspect storage when model placement matters:

   ```bash
   python3 scripts/vaws-top.py mounts 10.18.4.21
   ```

   Default text hides pseudo and container-overlay filesystems. JSON/MCP structured results retain the full mount list. `weight_candidate` is a heuristic; it does not recursively scan the remote filesystem.

4. Immediately before choosing the server, refresh only that host:

   ```bash
   python3 scripts/vaws-top.py status 10.18.4.21 --timeout 30
   ```

Do not follow a successful live result with a duplicate raw SSH occupancy query.

## CLI routing

```bash
python3 scripts/vaws-top.py servers
python3 scripts/vaws-top.py npu 10.18.4.21
python3 scripts/vaws-top.py npu 10.18.4.21 --ultra-compact
python3 scripts/vaws-top.py --json npu 10.18.4.21 --processes
python3 scripts/vaws-top.py --json npu 10.18.4.21 --process-details
```

- Add top-level `--json` for stable machine-readable output.
- `status HOST` is live by default. Use `status HOST --cache` to avoid a new probe; `--live` remains an explicit alias for the default.
- `--processes` includes compact PID, container, NPU memory, and ownership records.
- `--process-details` additionally includes pwd, command, executable, and user. Keep those details out of the default response unless they help answer the request.
- `npu --max-age N` prints the snapshot but exits `3` when it is missing or stale; it does not refresh implicitly.
- A lookup is an exact match on IP, display name, remote hostname, or server id. Ambiguous endpoints fail instead of guessing.

## MCP routing

Launch the dependency-free stdio server:

```text
python3 /absolute/path/to/vaws-top/scripts/vaws-top-mcp.py
```

Set `VAWS_TOP_URL=http://127.0.0.1:8789` only when the default is unsuitable. Route tool calls as follows:

- `find_npu_capacity`: shortlist hosts by idle NPUs, snapshot age, and tags; low-priority hosts sort last.
- `npu_status`: compact per-device utilization/HBM; supports `mode=cache|live`.
- `server_status`: experiment decision view with system, storage, Docker, processes, containers, and owners.
- `list_mounts`: mounted storage and likely weight locations.
- `list_npu_servers`: compact discovery/status list.

Tool text is concise; prefer `structuredContent` for downstream decisions. `mode=live` is still read-only to the caller but causes the monitor collector to contact the selected host.

## Service operation

On Linux, install or reconcile the user service with `./scripts/install-user-service.sh`. On Windows, use `scripts/install-windows-service.ps1`. Preserve the ignored `data/` directory across upgrades.

Read [references/acceptance.md](references/acceptance.md) when deploying, restarting, changing the Agent interface, or diagnosing health. User-facing CLI/API examples are in [docs/agent-access.md](../../../docs/agent-access.md).

## Result

Report the query mode (`cache` or requested `live`), snapshot age, decisive capacity/occupancy facts, and any relevant container, owner, or mount. Keep the default response compact and mention staleness or failed live collection explicitly.
