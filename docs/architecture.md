# Architecture

```text
Browser (127.0.0.1:8788)
  ├─ dashboard + history + server onboarding
  └─ /api proxy
        ↓ loopback only
Python API / adaptive scheduler (127.0.0.1:8789)
  ├─ viewer leases: fastest of 1s / 5s / 10s / 30s
  ├─ idle fallback: 120s
  ├─ history write floor: 30s
  ├─ infrastructure cadence: 60s
  └─ bounded parallel host probes
        ↓ project Ed25519 + OpenSSH multiplexing
Bare-metal NPU hosts
  ├─ npu-smi info + usages
  ├─ /proc/stat, /proc/loadavg, /proc/meminfo
  ├─ df + findmnt
  └─ docker ps + docker info
```

At startup, inventory discovery merges the active machine inventory with the workspace's complete host pool. Active records provide aliases and hardware tags. Addresses found only in the complete pool are still inserted and probed, but receive the derived `低优先级` tag. Only the first address field of each complete-pool line is parsed; adjacent authentication fields never enter application state. User-defined tags are stored in the existing `servers.tags_json` column and the derived priority tag is reconciled on each service start.

## Collection tiers

Fast collection reads CPU counters, load, memory and NPU state. CPU percentage is calculated from consecutive `/proc/stat` counters, so the probe does not sleep remotely. Infrastructure collection is separately cached because Docker and mount inspection are more expensive.

An NPU is busy when the host process table reports an owner, AICore utilization exceeds 1%, or HBM exceeds the configurable fallback threshold. The default is 8192 MB because the observed A3 fleet carries roughly 6 GB of idle driver HBM per device; the fallback therefore catches opaque cross-container occupancy without classifying driver overhead as a workload.

Each cycle is non-overlapping: a new cycle is scheduled only after the previous one finishes. Host probes run in a bounded thread pool, and OpenSSH `ControlPersist` reuses authenticated connections. Its control socket uses a short project-relative path so a deeply nested worktree cannot exceed the Unix-domain socket path limit. If a 1-second cycle cannot finish within one second, the system naturally runs at the achievable rate instead of creating a backlog.

On native Windows, the OpenSSH client does not use Unix-domain control sockets, so `ControlMaster`, `ControlPersist`, and `ControlPath` are omitted while all other host-key, identity, timeout, and keepalive controls remain active. The Windows installer runs the same Python supervisor under a per-user scheduled task triggered at logon; both processes continue to bind loopback only. The generated private key receives an explicit current-user-only Windows ACL.

## Persistence

SQLite runs in WAL mode. The primary history index is `(server_id, collected_at DESC)`, matching server-range reports, with a second time index for retention cleanup and fleet reports. Detailed snapshots are stored as JSON alongside summary columns used by aggregate queries. The heatmap endpoint groups summary columns and `payload_json.devices` with SQLite JSON functions into timezone-aligned two-hour buckets, so the UI can render both server-level and per-NPU activity without loading raw samples into the browser.

Passwords are deliberately absent from every schema. The API accepts them only for the duration of a batch request and passes one candidate at a time to the workspace bootstrap entrypoint through stdin.
