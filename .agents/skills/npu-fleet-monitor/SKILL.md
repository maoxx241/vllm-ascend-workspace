---
name: npu-fleet-monitor
description: Clone or locate the standalone vaws-top repository and provide its basic CLI/MCP query entrypoints. Use when vaws-top is not yet available, for basic fleet discovery and server inspection, or to deploy, inspect, restart, or stop its local loopback service. Do not use to allocate NPUs, choose a task identity, kill processes, or treat fleet inventory as authority. Detailed fleet-query guidance lives in the standalone repository skill.
---

# vaws-top entry

Keep the application, its runtime, and its complete Agent instructions in the standalone `vllm-ascend-workspace/vaws-top` repository. This scaffold Skill only locates or bootstraps that checkout and hands off to its published entrypoints.

The intended consumption is `uvx vaws-top`. Custom checkout paths and configuration overrides must be explicit (`--clone-dir`, `VAWS_TOP_ROOT`, `--inventory-files`, `--host-pool-files`, `--bootstrap-command`). The deploy script in this skill is landing on the package plane in a parallel change.

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

`ensure` is the only action that may clone a missing checkout or write consumer-owned `NFM_*` keys. `status`, `restart`, `stop`, metadata reads, and help never clone, fetch, checkout, install, or build. If the documented default directory is still a legacy scaffold `vaws-top` worktree, stop and request a separate `--clone-dir` or `VAWS_TOP_ROOT`; do not change its origin, reset it, delete it, detach it, move runtime data, or import keys.

The final JSON includes `clone`, `source_path`, `repository`, `ref`, `commit`, and `agent_skill`. Use the returned `clone` as `<vaws-top>` below. `allocation_authority` is always false. Coordinator execution leases remain authoritative; monitor presence does not confer a task identity, NPU lease, or cleanup right.

## External entrypoints

The standalone repository owns these paths. Do not copy its advanced skill into the scaffold.

- Agent skill: `<vaws-top>/.agents/skills/vaws-top/SKILL.md`
- CLI: `<vaws-top>/scripts/vaws-top.py`
- MCP: `<vaws-top>/scripts/vaws-top-mcp.py`
- Start: `<vaws-top>/scripts/start.sh`
- Linux user-service installer: `<vaws-top>/scripts/install-user-service.sh`

The systemd unit loads `<vaws-top>/.env` through `EnvironmentFile`. `start.sh` does not source that file. Consumer-owned keys are only `NFM_INVENTORY_FILES`, optional `NFM_HOST_POOL_FILES`, and `NFM_BOOTSTRAP_COMMAND`. Preserve `NFM_STATE_DIR`, bind settings, credentials, and any other existing values. The extracted monitor ignores `NFM_SOURCE_WORKSPACE`. Pass inventory file paths; never print inventory contents, `.env` secrets, or private keys.

## Basic CLI

```bash
python3 <vaws-top>/scripts/vaws-top.py servers
python3 <vaws-top>/scripts/vaws-top.py capacity --min-idle 4 --max-age 180
python3 <vaws-top>/scripts/vaws-top.py status HOST
python3 <vaws-top>/scripts/vaws-top.py status HOST --cache
python3 <vaws-top>/scripts/vaws-top.py mounts HOST
python3 <vaws-top>/scripts/vaws-top.py --json npu HOST --process-details
```

`status HOST` is live by default; add `--cache` when stored data is sufficient. `servers`, `capacity`, `mounts`, and `npu` use cached observations by default; commands that support it accept `--live`. Add `--json` for structured output. A live query asks the centralized service to probe once; do not follow a successful result with ad hoc SSH. Capacity is observed availability, not a reservation. vaws-top output must not be used to decide device allocation; use the coordinator's host queue.

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

Keep listeners on `127.0.0.1`, preserve the clone's ignored `data/` and `.env`, and never use this entry to launch workloads or reserve NPUs.
