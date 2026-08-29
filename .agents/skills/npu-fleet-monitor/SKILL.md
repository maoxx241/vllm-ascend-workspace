---
name: npu-fleet-monitor
description: Bootstrap or locate the standalone vaws-top worktree and provide its basic CLI/MCP query entrypoints. Use when vaws-top is not yet available, for basic fleet discovery and server inspection, or to deploy, inspect, restart, or stop its local service. Detailed fleet-query guidance lives on the vaws-top branch.
---

# vaws-top entry

Keep the application and its complete Agent instructions on the standalone `vaws-top` branch. This main-branch Skill is only the bootstrap entry.

Run the helper on the host execution plane. Deploy or reconcile:

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py ensure
```

Locate, inspect, restart, or stop:

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py status
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py restart
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py stop
```

The final JSON includes `worktree` and `agent_skill`. Use the returned `worktree` as `<vaws-top>` below.

## Basic CLI

```bash
python3 <vaws-top>/scripts/vaws-top.py servers
python3 <vaws-top>/scripts/vaws-top.py capacity --min-idle 4 --max-age 180
python3 <vaws-top>/scripts/vaws-top.py status HOST
python3 <vaws-top>/scripts/vaws-top.py status HOST --cache
python3 <vaws-top>/scripts/vaws-top.py mounts HOST
python3 <vaws-top>/scripts/vaws-top.py --json npu HOST --process-details
```

`status HOST` is live by default; add `--cache` when stored data is sufficient. `servers`, `capacity`, `mounts`, and `npu` use cached observations by default; commands that support it accept `--live`. Add `--json` for structured output. A live query asks the centralized service to probe once; do not follow a successful result with ad hoc SSH. Capacity is observed availability, not a reservation.

## Basic MCP

Run the stdio server directly or register it in the Agent's MCP configuration:

```toml
[mcp_servers.vaws_top]
command = "python3"
args = ["<vaws-top>/scripts/vaws-top-mcp.py"]
env = { VAWS_TOP_URL = "http://127.0.0.1:8789" }
```

The basic tools are `list_npu_servers`, `find_npu_capacity`, `server_status`, `npu_status`, and `list_mounts`. Host-query tools default to cached data in MCP; pass `mode="live"` for a fresh centralized probe.

Before advanced fleet selection, process attribution, mount discovery, or operational changes, read the returned `agent_skill` completely and follow it.

Keep listeners on `127.0.0.1`, preserve the worktree's ignored `data/`, and never use this entry to launch workloads or reserve NPUs.
