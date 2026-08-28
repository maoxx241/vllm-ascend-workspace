---
name: npu-fleet-monitor
description: Create or reuse the dedicated vaws-top worktree, build the dashboard, install or restart its loopback-only user service, and report local health. Use for requests to deploy, start, inspect, restart, or stop the workspace NPU fleet monitor. Do not use to launch workloads or manage remote development containers.
---

# NPU Fleet Monitor

Operate the local monitoring dashboard without mixing its standalone project history into `main`.

## Invariants

- Keep application source on the standalone `vaws-top` branch; retain its independent project history.
- Reuse an existing clean worktree attached to that branch. The helper creates the default independent worktree only when none exists.
- Run the public helper outside the filesystem sandbox from its first call. It invokes the user systemd manager and starts a service that uses OpenSSH to probe managed hosts.
- Keep the web and API listeners on `127.0.0.1`. This service has no web login and is intended only for the local machine.
- Preserve the worktree's ignored `data/` directory across rebuilds. It contains historical SQLite data, a monitor-specific SSH key, and `known_hosts`; never print, stage, or copy those secrets into tracked files.
- Reuse the workspace machine inventory and device-management utilities. Use `machine-management` separately when the inventory itself needs to change.
- Do not use this workflow to start remote jobs, replace containers, or reserve NPUs.

## Commands

Deploy or reconcile the service:

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py ensure
```

The helper fetches `vaws-top` from a configured remote when needed, finds or creates its worktree, validates a clean source tree, runs the locked build and backend tests when the commit changed, installs the user unit, restarts it, and waits for `/api/health`.

Inspect, restart, or stop it:

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py status
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py restart
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py stop
```

Pass `--worktree /absolute/path` only when the default or discovered worktree is unsuitable. Pass `--branch` only for an intentional alternate monitor branch.

Read [references/acceptance.md](references/acceptance.md) when validating a deployment or diagnosing a failed health check. User-facing setup and operating notes are in [`docs/npu-fleet-monitor.md`](../../../docs/npu-fleet-monitor.md).

## Result

Report the source branch and commit, worktree path, unit state, local URL, health result, and whether a build ran. On success the enabled user unit survives terminal closure; browser activity controls 1/5/10/30-second sampling, while no active page returns the collector to its low-frequency interval.
