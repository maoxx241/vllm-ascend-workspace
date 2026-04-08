---
name: vllm-ascend-serving
description: Start, check, or stop a single-node vLLM Ascend online service on a workspace-managed ready remote container. Use for requests like "拉服务", "在远端起个服务", "重启服务", "看服务状态", "停掉服务". Do not use for machine attach, environment bootstrap, code sync, benchmark orchestration, or offline inference.
---

# vLLM Ascend Serving

Manage the lifecycle of a **single-node colocated** `vllm-ascend` online service on a workspace-managed ready remote container.

This skill takes structured parameters, handles all SSH escaping and remote execution internally, and returns machine-readable JSON. The agent never needs to construct raw shell commands for service management.

## Use this skill when

- the user asks to start / launch / pull up a vllm-ascend service on a managed machine
- the user asks to restart or relaunch a service (possibly with changed flags or env)
- the user asks to check if a running service is alive / ready
- the user asks to stop a running service

## Do not use this skill when

- the task is adding, verifying, repairing, or removing a machine (use `machine-management`)
- the task is syncing code to the remote container (use `remote-code-parity`)
- the task is running benchmarks (a separate skill's responsibility)
- the task is offline inference
- the machine is not yet ready in inventory

## Critical rules

- `start` automatically runs `remote-code-parity` before launching. If parity fails, start is blocked.
- `status` and `stop` do not require parity.
- All remote execution goes through the scripts — never construct raw SSH commands for serving.
- Keep local runtime state only under `.vaws-local/serving/`.
- Progress on `stderr` as `__VAWS_SERVING_PROGRESS__=<json>`, final result on `stdout` as JSON.

## Cross-platform launcher rule

- macOS / Linux / WSL: `python3 ...`
- Windows: `py -3 ...`

## Public entry points

### Start a service

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine <alias-or-ip> \
  --model <remote-weight-path> \
  [--served-model-name <name>] \
  [--tp <N>] [--dp <N>] \
  [--devices <0,1,2,3>] \
  [--extra-env KEY=VALUE ...] \
  [--port <N>] \
  [--health-timeout <seconds>] \
  [-- <extra vllm serve args>]
```

### Relaunch with previous config

```bash
# Exact same config
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine <alias> --relaunch

# Add a debug env
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine <alias> --relaunch --extra-env VLLM_LOGGING_LEVEL=DEBUG

# Remove an env from previous config
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine <alias> --relaunch --unset-env MY_DEBUG_FLAG

# Remove a vllm arg from previous config (use = to avoid argparse ambiguity)
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine <alias> --relaunch --unset-args=--enforce-eager

# Relaunch with a different model
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine <alias> --relaunch --model /data/models/OtherModel
```

### Probe NPU device availability

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py \
  --machine <alias-or-ip>
```

Returns which NPU devices are free, which are busy (with PID and HBM details), probed on the bare-metal host for cross-container visibility.

### Check status

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_status.py \
  --machine <alias-or-ip>
```

### Stop

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py \
  --machine <alias-or-ip> [--force]
```

## Local state

Per-machine launch state is stored under `.vaws-local/serving/<alias>.json`.

This file records the last successful launch parameters (model, tp, devices, env, extra args, port, pid, log paths). It is the basis for `--relaunch`.

## Workflow

### 1. Resolve the target machine

The `--machine` argument is looked up in the local machine inventory. The machine must already be managed and ready.

### 2. Stop any existing service

If a previous service is recorded for this machine, it is stopped before launching a new one.

### 3. Run remote-code-parity (start only)

Unless `--skip-parity` is passed, `parity_sync.py` is called to ensure the container has the current local code. If parity fails, start is blocked.

### 4. Probe NPUs

NPU availability is checked via `npu-smi info` on the **bare-metal host** (not the container). Host-level probing sees processes from all containers, bypassing PID namespace isolation. Devices with HBM usage above 4 GB are also marked busy to catch cross-container occupancy:

- If `--devices` is specified, those devices are verified to be free. If any are busy, start is blocked with the conflict details.
- If `--devices` is not specified but `--tp` is given, the first N free devices are automatically selected.
- If NPU probe fails (e.g. driver issue), it is treated as a non-fatal warning and launch continues with user-specified devices.

### 5. Validate and launch

- Model path is checked for existence on the remote container.
- A free port is auto-detected (or the explicit `--port` is used).
- A bash launch script is built internally with proper escaping — the agent never sees or edits this script.
- The process is started via `nohup` + `disown` and detached from the SSH session.

### 6. Wait for readiness

The script polls `/health` and `/v1/models` until both return success or the timeout expires.

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
  "machine": "blue-a",
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
