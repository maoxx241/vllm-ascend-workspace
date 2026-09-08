---
name: npu-fleet-monitor
description: Start, inspect, restart, or stop the loopback-only vaws-top NPU fleet monitor as a local uvx process, and provide its basic CLI/MCP query entrypoints. Use when the local dashboard is needed, for basic fleet discovery and server inspection, or to check whether the monitor is up. Do not use to allocate NPUs, choose a task identity, kill processes, or treat fleet inventory as authority. Detailed fleet-query guidance lives in the standalone vaws-top repository skill.
---

# vaws-top entry

The dashboard, its runtime, and its complete Agent instructions live in the published package `vaws-top` from `vllm-ascend-workspace/vaws-top`. This scaffold Skill only launches that package through `uvx` as a local background process and hands off to its CLI/MCP.

The pinned revision is the single constant `VAWS_TOP_REF` in `scripts/manage_monitor.py` (currently `v0.1.0`); the wheel filename is derived from it. Every invocation uses the GitHub Release wheel:

```bash
uvx --from "https://github.com/vllm-ascend-workspace/vaws-top/releases/download/v0.1.0/vaws_top-0.1.0-py3-none-any.whl" vaws-top ...
```

Why the wheel and not `git+https://...@v0.1.0`: vaws-top ships a JS frontend whose build output exists only in the released wheel. Installing from git runs a hatch hook that needs Node.js; on a host without Node it silently produces a wheel with no frontend and `serve` fails at startup. This is specific to vaws-top; pure-Python sibling packages keep `git+tag`. Operational contract: **every new vaws-top release must upload its wheel as a Release asset, otherwise the monitor cannot be installed** by this Skill.

Developers may override the install source with `--from <spec>` or `VAWS_TOP_FROM=<spec>` (a local wheel, a source tree, or `git+https://...`, which needs Node.js 22.13+). The default is always the wheel. `uvx` fetches and caches the package; there is no checkout, no pin file, and no service manager.

Run the helper on the host execution plane:

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py deploy    # install/cache and prove the packaged frontend exists; no service
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py start     # background `vaws-top serve`
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py status
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py restart
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py stop
```

`deploy` runs `require_static()` inside the installed package environment, the same check `serve` performs at startup, so a frontend-less install fails there with the package's own message instead of at the first `start`. Run it first on a fresh machine.

Options: `--port` (default `8788`), `--wait-seconds` (how long `start` waits for `/api/health`, default 90), `--from` (install source override), and explicit overrides `--inventory-files`, `--host-pool-files`, `--bootstrap-command`. Precedence for those three `NFM_*` keys is flag, then caller environment, then scaffold default (shared `.vaws-local/machine-inventory.json`, `hosts.txt` if present, and `machine-management`'s `bootstrap-host-key --password-stdin`). Pass file paths; never print inventory contents, private keys, or passwords.

Runtime state is untracked under the primary worktree's `.vaws-local/npu-fleet-monitor/`: `serve.json` (pid, port, spec), `serve.log`, and `data/` (`NFM_STATE_DIR`: SQLite history, dedicated Ed25519 key, `known_hosts`). `stop` signals the whole process group recorded in `serve.json`; `start` is idempotent while that pid is alive and reports the running instance's spec.

Final JSON on stdout includes `ok`, `url`, `health`, `pid`, `spec`, `default_spec`, `ref`, `cli_prefix`, `mcp_command`, `state_dir`, and `log`. `allocation_authority` is always false. Coordinator execution leases remain authoritative; monitor presence does not confer a task identity, NPU lease, or cleanup right.

## Loopback only

The helper always passes `--bind 127.0.0.1` and sets `NFM_BIND=127.0.0.1`; there is no flag to change it. The service has no login and serves only the local user. Do not expose it through port forwarding, a reverse proxy, or a container port.

## Basic CLI

Use the `cli_prefix` from the JSON, which is the `uvx` command above:

```bash
WHEEL="https://github.com/vllm-ascend-workspace/vaws-top/releases/download/v0.1.0/vaws_top-0.1.0-py3-none-any.whl"
uvx --from "$WHEEL" vaws-top servers
uvx --from "$WHEEL" vaws-top capacity --min-idle 4 --max-age 180
uvx --from "$WHEEL" vaws-top status HOST
uvx --from "$WHEEL" vaws-top status HOST --cache
uvx --from "$WHEEL" vaws-top mounts HOST
uvx --from "$WHEEL" vaws-top --json npu HOST --process-details
```

`status HOST` is live by default; add `--cache` when stored data is sufficient. `servers`, `capacity`, `mounts`, and `npu` use cached observations by default; commands that support it accept `--live`. Add `--json` for structured output. Pass `--url http://127.0.0.1:<port>` when the service runs on a non-default port. A live query asks the running service to probe once; do not follow a successful result with ad hoc SSH. Capacity is observed availability, not a reservation. vaws-top output must not be used to decide device allocation; use the coordinator's host queue.

## Basic MCP

Register the stdio server with the `mcp_command` from the JSON:

```json
{
  "mcpServers": {
    "vaws-top": {
      "command": "uvx",
      "args": ["--from", "https://github.com/vllm-ascend-workspace/vaws-top/releases/download/v0.1.0/vaws_top-0.1.0-py3-none-any.whl", "vaws-top", "mcp"],
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
