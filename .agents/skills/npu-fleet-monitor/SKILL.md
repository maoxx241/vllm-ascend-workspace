---
name: npu-fleet-monitor
description: Start, check, restart or stop this workspace's local vaws-top monitor through uvx. Fleet-query methods belong to the monitor package's own skill.
---

# Local fleet monitor

Use `scripts/manage_monitor.py` for the local service lifecycle:

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py deploy
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py start
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py status
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py restart
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py stop
```

`deploy` verifies the released wheel's frontend. `start` reuses a running
instance and checks health. The helper owns its local process record; preserve
its untracked `.vaws-local/npu-fleet-monitor/` state across upgrades.
On Windows, use `uv run python` for these commands. Background launch hides
the console; status uses a read-only process handle and stop terminates the
owned process tree with `taskkill`.

The result provides the URL, logs, `cli_prefix`, `mcp_command` and `skill_url`.
Use the package's linked skill for fleet-query methods and interpretation;
this workspace does not maintain another copy. `--help` lists install-source,
port and inventory overrides. Host-key bootstrap is explicitly configured via
`--bootstrap-command` or `NFM_BOOTSTRAP_COMMAND`; no workspace machine manager
is assumed.

Keep the service on loopback. Fleet data is observation, not device allocation
or authority to stop remote processes. Managed runs use coordinator directly
and do not require a monitor query first.
