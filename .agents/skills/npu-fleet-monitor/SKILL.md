---
name: npu-fleet-monitor
description: Start, inspect, restart, or stop the loopback-only vaws-top NPU fleet monitor as a local uvx process, and provide its basic CLI/MCP query entrypoints. Use when the local dashboard is needed, for basic fleet discovery and server inspection, or to check whether the monitor is up. Do not use to allocate NPUs, choose a task identity, kill processes, or treat fleet inventory as authority. Detailed fleet-query guidance lives in the standalone vaws-top repository skill.
---

# vaws-top entry

The dashboard, its runtime, and its complete Agent instructions live in the published package `vaws-top` from `vllm-ascend-workspace/vaws-top`. This scaffold Skill only launches that package through `uvx` as a local background process and hands off to its CLI/MCP.

The pinned revision is the constant `VAWS_TOP_REF` in `scripts/manage_monitor.py` (currently `v0.1.0`). Every invocation uses exactly:

```bash
uvx --from "git+https://github.com/vllm-ascend-workspace/vaws-top@v0.1.0" vaws-top ...
```

`uvx` fetches, builds, and caches the package itself. Building from git needs Node.js 22.13+ once on the machine (the frontend is compiled into the wheel); after that the cache is reused. There is no checkout, no pin file, and no service manager.

Run the helper on the host execution plane:

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py deploy    # resolve/build/cache only, no service
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py start     # background `vaws-top serve`
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py status
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py restart
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py stop
```

Options: `--port` (default `8788`), `--wait-seconds` (how long `start` waits for `/api/health`, default 90), and explicit overrides `--inventory-files`, `--host-pool-files`, `--bootstrap-command`. Precedence for those three `NFM_*` keys is flag, then caller environment, then scaffold default (shared `.vaws-local/machine-inventory.json`, `hosts.txt` if present, and `machine-management`'s `bootstrap-host-key --password-stdin`). Pass file paths; never print inventory contents, private keys, or passwords.

Runtime state is untracked under the primary worktree's `.vaws-local/npu-fleet-monitor/`: `serve.json` (pid, port, spec), `serve.log`, and `data/` (`NFM_STATE_DIR`: SQLite history, dedicated Ed25519 key, `known_hosts`). `stop` signals the whole process group recorded in `serve.json`; `start` is idempotent while that pid is alive.

Final JSON on stdout includes `ok`, `url`, `health`, `pid`, `spec`, `ref`, `cli_prefix`, `mcp_command`, `state_dir`, and `log`. `allocation_authority` is always false. Coordinator execution leases remain authoritative; monitor presence does not confer a task identity, NPU lease, or cleanup right.

## Loopback only

The helper always passes `--bind 127.0.0.1` and sets `NFM_BIND=127.0.0.1`; there is no flag to change it. The service has no login and serves only the local user. Do not expose it through port forwarding, a reverse proxy, or a container port.

## Basic CLI

Use the `cli_prefix` from the JSON, which is the `uvx` command above:

```bash
uvx --from "git+https://github.com/vllm-ascend-workspace/vaws-top@v0.1.0" vaws-top servers
uvx --from "git+https://github.com/vllm-ascend-workspace/vaws-top@v0.1.0" vaws-top capacity --min-idle 4 --max-age 180
uvx --from "git+https://github.com/vllm-ascend-workspace/vaws-top@v0.1.0" vaws-top status HOST
uvx --from "git+https://github.com/vllm-ascend-workspace/vaws-top@v0.1.0" vaws-top status HOST --cache
uvx --from "git+https://github.com/vllm-ascend-workspace/vaws-top@v0.1.0" vaws-top mounts HOST
uvx --from "git+https://github.com/vllm-ascend-workspace/vaws-top@v0.1.0" vaws-top --json npu HOST --process-details
```

`status HOST` is live by default; add `--cache` when stored data is sufficient. `servers`, `capacity`, `mounts`, and `npu` use cached observations by default; commands that support it accept `--live`. Add `--json` for structured output. Pass `--url http://127.0.0.1:<port>` when the service runs on a non-default port. A live query asks the running service to probe once; do not follow a successful result with ad hoc SSH. Capacity is observed availability, not a reservation. vaws-top output must not be used to decide device allocation; use the coordinator's host queue.

## Basic MCP

Register the stdio server with the `mcp_command` from the JSON:

```json
{
  "mcpServers": {
    "vaws-top": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/vllm-ascend-workspace/vaws-top@v0.1.0", "vaws-top", "mcp"],
      "env": { "VAWS_TOP_URL": "http://127.0.0.1:8788" }
    }
  }
}
```

The basic tools are `list_npu_servers`, `find_npu_capacity`, `server_status`, `npu_status`, and `list_mounts`. Host-query tools default to cached data in MCP; pass `mode="live"` for a fresh centralized probe.

## Advanced guidance

Before advanced fleet selection, process attribution, mount discovery, or operational changes, read the standalone repository's skill and Agent contract at the pinned tag and follow them:

- <https://github.com/vllm-ascend-workspace/vaws-top/blob/v0.1.0/.agents/skills/vaws-top/SKILL.md>
- <https://github.com/vllm-ascend-workspace/vaws-top/blob/v0.1.0/docs/agent-access.md>

Do not copy that skill into the scaffold. Never use this entry to launch workloads or reserve NPUs.
