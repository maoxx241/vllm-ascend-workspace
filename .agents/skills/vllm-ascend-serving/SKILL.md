---
name: vllm-ascend-serving
description: Start, check, or stop a single-node vLLM Ascend online service on a workspace-managed ready remote container. Use for requests like "拉服务", "在远端起个服务", "重启服务", "看服务状态", "停掉服务". Do not use for machine attach, environment bootstrap, code sync, benchmark orchestration, or offline inference.
---

# vLLM Ascend Serving

Manage the lifecycle of a **single-node colocated** `vllm-ascend` online service on an isolated VAWS session container.

The optional [prepared-runtime MCP](../../coordinator/README.md) has a separate
binding/execution lifecycle. Its bindings are not legacy session records and
must not be fed to `serve_start.py --session-id`. In that mode use the
coordinator's request/preflight/activate/heartbeat/release sequence and existing
remote-dev job tools, with pinned source, declared service ports and a new
service process. The legacy wrappers here are not yet transparent adapters for
pool bindings. Do not allocate a second local NPU lease for the pool run.

Remote substrate rule: use `.remote-dev` remote tools for ad hoc remote
read/edit/bash/search/patch work around a service. Use this skill for the
domain service lifecycle contract and keep its scripts as the compatibility
backend for managed VAWS sessions.

This skill takes structured parameters, handles all SSH escaping and remote execution internally, and returns machine-readable JSON. The agent never needs to construct raw shell commands for service management.

## Use this skill when

- the user asks to start / launch / pull up a vllm-ascend service in a managed session
- the user asks to restart or relaunch a service (possibly with changed flags or env)
- the user asks to check if a running service is alive / ready
- the user asks to stop a running service
- another skill needs to start a service (e.g. `ascend-memory-profiling`)

## Do not use this skill when

- the task is adding, verifying, repairing, or removing a machine (use `machine-management`)
- the task is syncing code to the remote container (use `remote-code-parity`)
- the task is running benchmarks (a separate skill's responsibility)
- the task is offline inference
- no session exists yet for the target (use `session-management` first)

## Critical rules

- Serving is **session-only**. `serve_start.py`, `serve_status.py`, and `serve_stop.py` take an optional `--session-id <id>` / `--session-file <path>`. When both are omitted, the session is auto-resolved by walking up from the current working directory to the nearest `.vaws-local/current-session.json` worktree binding — running from inside a session worktree needs zero target arguments. If no binding is found, the command fails fast with instructions to pass `--session-id` or create a session with `session-management`'s `session_create.py`.
- `start` automatically runs `remote-code-parity` before launching. If parity fails, start is blocked.
- `status` and `stop` do not require parity.
- Each session reads and writes only `.vaws-local/sessions/<id>/serving.json` and never stops another session's service.
- `start` / `stop` operations for the same session are serialized with a serving lock; different sessions remain independent.
- Once a remote PID is launched, `serve_start.py` writes `serving.json` with `status=starting` before health probing so `serve_stop.py` can clean up even if readiness later fails.
- Service ports are always allocated and released through the session lease mechanism — there is no ad-hoc free-port scanning.
- All remote execution goes through the scripts — never construct raw SSH commands for serving.
- Keep local runtime state under `.vaws-local/sessions/<id>/`.
- Progress on `stderr` as `__VAWS_SERVING_PROGRESS__=<json>`, final result on `stdout` as JSON.
- With a unified workspace alias, new runtime directories use `.vaws-runtime/serving/<alias>/<timestamp>/` and the service receives `VAWS_AGENT_ID`, `VAWS_AGENT_ALIAS`, and `VAWS_PROJECT_ALIAS`. Without an alias, preserve the legacy layout.

## Cross-platform launcher rule

- macOS / Linux / WSL: `python3 ...`
- Windows: `py -3 ...`

## Public entry points

### Start a service

```bash
# Inside a session worktree the session is auto-resolved — no target flag needed.
# Outside a worktree, pass --session-id <id> (or --session-file <path>).
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  [--session-id <id> | --session-file <path>] \
  --model <remote-weight-path> \
  [--preset <name>] \
  [--served-model-name <name>] \
  [--tp <N>] [--dp <N>] \
  [--devices <0,1,2,3>] \
  [--extra-env KEY=VALUE ...] \
  [--port <N>] \
  [--health-timeout <seconds>] \
  [--wrap-script <remote-path>] \
  [--skip-parity] \
  [-- <extra vllm serve args>]
```

#### Serving presets

`--preset <name>` applies a named, verified recipe from `presets/<name>.json`
(tp/dp/port/devices/served-model-name/health-timeout/env/serve_args, plus an
optional `vllm_version` pin). Explicit CLI args always override preset values;
preset `env` merges under `--extra-env` per key; preset `serve_args` apply only
when no `--` passthrough is given. Presets never carry a model weight path —
`--model` stays required.

When a preset is used, a **preflight** runs before any existing service is
stopped: the pinned `vllm_version` is compared against the container's actual
vllm, absolute `PYTHONPATH` entries must exist in the container, and
JSON-valued serve args (`--additional-config`, `--model-loader-extra-config`,
`--speculative-config`, `--compilation-config`) must parse. A failed preflight
aborts with `phase: preflight` and leaves the running service untouched, so
recipe/version drift never costs a multi-minute model load. Available presets:
`dsv4-flash` (DeepSeek-V4-Flash W4A8 MTP, verified on A3 nightly CANN 9.1 /
vllm 0.26.0).

#### Staged readiness

Readiness is not `/health` alone. `wait_for_ready` tracks startup phases from
runtime-log markers (`weight-load` → `compile`/`graph-capture` → `health-ok` →
`models-ok`), then requires one deterministic real request
(`first-token-ok`, temperature 0) before reporting ready. The readiness output
records the phase timeline; a timeout names the last observed phase (e.g.
`graph-capture`, which legitimately takes 15–20 minutes on large models) so a
stuck launch is diagnosable instead of an opaque 300s timeout. A lost SSH
probe round-trip is treated as unknown, never as process exit.

#### Launch wrapping (`--wrap-script`)

The serving skill supports a generic `--wrap-script` mechanism. When provided, the vLLM launch command is written as `_serve.sh` in the runtime directory, and the wrapper script is called with two arguments: `$1` = serve script path, `$2` = runtime directory.

This is used by other skills (e.g. `ascend-memory-profiling`) to wrap the service launch process without the serving skill needing to know the wrapping details. The serving skill is agnostic to what the wrapper does.

The `wrap_script` path is recorded in the serving state so downstream skills can detect it.

### Relaunch with previous config

```bash
# Exact same config (inside a session worktree — session auto-resolved)
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py --relaunch

# Add a debug env
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --relaunch --extra-env VLLM_LOGGING_LEVEL=DEBUG

# Remove an env from previous config
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --relaunch --unset-env MY_DEBUG_FLAG

# Remove a vllm arg from previous config (use = to avoid argparse ambiguity)
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --relaunch --unset-args=--enforce-eager

# Relaunch with a different model, targeting an explicit session
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --session-id <id> --relaunch --model /data/models/OtherModel
```

### Probe NPU device availability

`serve_probe_npus.py` is the one entry point with two mutually exclusive target surfaces:

```bash
# Probe the session's base host (inside a session worktree — auto-resolved)
python3 .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py

# Explicit session target
python3 .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py \
  --session-id <id>

# Resource-pool probe of a registered machine host (machine-management scope)
python3 .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py \
  --machine <alias-or-ip>
```

Returns which NPU devices are free, which are busy (with PID and HBM details), probed on the bare-metal host for cross-container visibility.

### Check status

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_status.py \
  [--session-id <id> | --session-file <path>]
```

### Stop

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py \
  [--session-id <id> | --session-file <path>] [--force]
```

## Local state

Session launch state is stored under `.vaws-local/sessions/<session-id>/serving.json` — this is the only serving state location.

This file records the last successful launch parameters (model, tp, devices,
env, extra args, port, pid, log paths, runtime_dir, wrap_script) plus the
workspace `agent_id` and alias snapshot. It is the basis for `--relaunch` and
is read by other skills (e.g. `ascend-memory-profiling`) in attach mode.

During launch the same file may temporarily contain `status=starting`; this is still a valid cleanup target for `serve_stop.py`.

## Workflow

### 1. Resolve the target session

The session comes from `--session-id` / `--session-file`, or is auto-resolved from the nearest `.vaws-local/current-session.json` worktree binding (walking up from the current working directory). If neither is given and no binding is found, the command fails fast and tells the user to pass `--session-id` or create a session with `session_create.py`.

### 2. Stop any existing service

If a previous service is recorded for this session, it is stopped before launching a new one. The target is the session, not the base machine, so other sessions on the same host are not touched.

### 3. Run remote-code-parity (start only)

Unless `--skip-parity` is passed, `parity_sync.py` is called to ensure the container has the current local code. Parity statuses `ready`, `skipped` (explicit image mode), and `materialized` count as success (`materialized` is what `auto` returns after pure-Python runtime updates). `source-only` and `dry-run` block startup: publishing to the cache does not update the execution tree.

Note: if a previous service process survives SIGINT+SIGTERM+SIGKILL, start fails fast instead of launching a second instance against the same port/devices.

### 4. Probe NPUs

NPU availability is checked via `npu-smi info` on the **bare-metal host** (not the container). Host-level probing sees processes from all containers, bypassing PID namespace isolation. Devices with HBM usage above 4 GB are also marked busy to catch cross-container occupancy:

- If `--devices` is specified, those devices are verified to be free. If any are busy, start is blocked with the conflict details.
- If `--devices` is not specified but `--tp` is given, the first N free devices are automatically selected, where N = TP × DP (defaults to TP when DP is not set).
- If NPU probe fails (e.g. driver issue), it is treated as a non-fatal warning and launch continues with user-specified devices.

### 5. Validate and launch

- Model path is checked for existence on the remote container.
- The service port is allocated through the session port lease (or the explicit `--port` is used); there is no ad-hoc free-port scanning.
- A bash launch script is built internally with proper escaping — the agent never sees or edits this script.
- The process is started via `nohup` + `disown` and detached from the SSH session.

### 6. Wait for readiness

The script polls `/health` and `/v1/models` while tracking startup phases from
runtime-log markers, then requires one deterministic real request before
reporting ready. A timeout reports the last observed phase.

### 6a. Diagnose launch failure before any code change

If the service fails during engine initialization or health check timeout:

- Read **both** `stdout.log` and `stderr.log` from the remote runtime directory — vllm often logs the actual Python exception to stdout, not stderr.
- Identify the actual exception type and message before hypothesizing a cause.
- Do not modify source code to work around a launch failure until the root cause is confirmed from logs.
- If the root cause is unclear, try the simplest launch configuration first (e.g. tp-only, no speculative decoding, no graph mode) and incrementally add features to isolate the failing component.

### 7. Return structured JSON

On success:

```json
{
  "status": "ready",
  "session_id": "pr123",
  "base_url": "http://10.0.0.8:38721",
  "port": 38721,
  "pid": 12345,
  "served_model_name": "Qwen3-32B",
  "model": "/data/models/Qwen3-32B",
  "log_stdout": "/vllm-workspace/.vaws-runtime/serving/.../stdout.log",
  "log_stderr": "/vllm-workspace/.vaws-runtime/serving/.../stderr.log"
}
```

On failure, includes `stderr_tail` for diagnosis.

## Reference files

- `.agents/skills/vllm-ascend-serving/references/behavior.md`
- `.agents/skills/vllm-ascend-serving/references/command-recipes.md`
- `.agents/skills/vllm-ascend-serving/references/acceptance.md`
# Active NPU ownership

Managed serving and relaunch require a nonempty live NPU lease matching the
session snapshot before touching an existing service. Do not select free cards
as a fallback for an empty lease, and do not trust a stale `session.json` after
its live lease was released.
