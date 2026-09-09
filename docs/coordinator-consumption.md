# Consuming vaws-coordinator

Status: current

The shared runtime pool, local task registry and four `vaws_*` tools live in
the public package
[`vaws-coordinator`](https://github.com/vllm-ascend-workspace/vaws-coordinator)
(`v0.2.0`, locked by `uv.lock`). This scaffold imports that package. It does
not clone a checkout and does not read a former checkout-root environment variable.

This document is the consumer-side contract. See
[dependency-plane.md](dependency-plane.md) for install and capability
reporting.

## 1. Why a package

The coordinator is run as `python -m vaws_coordinator task-server` and
`python -m vaws_coordinator.vaws`. A checkout plus `sys.path` was the
previous model; it collided with the scaffold's own
`.agents/lib/vaws_coordinator.py`. That file is now
`vaws_coordinator_launch.py`. The package is the only `vaws_coordinator`
import.

**Consequence:** cloning this scaffold without `uv sync` does not give you
the task tools. Local docs and Git work are unaffected. Missing packages
return `blocked` / `unavailable` with `uv sync`. Nothing falls back to
in-tree copies of the moved writers.

## 2. Installing

```bash
uv sync
python3 .agents/scripts/vaws.py status
python3 .agents/scripts/vaws.py env --json
```

`uv.lock` records the git commit. Do not copy that SHA into workflows.

## 3. What the scaffold injects

| Variable | Value | Why |
|---|---|---|
| `VAWS_AGENT_SESSIONS_DIR` | `<shared workspace>/.vaws-local/agent-sessions` | One local task registry. |
| `VAWS_HOST_QUEUE_MODULE` | unset by this scaffold | Host NPU authority is the package's `vaws_coordinator.host.vaws_npu_coordination`. The env var is an override. |
| `VAWS_COORDINATOR_STATE_DIR` | unset by this scaffold; defaults to `<shared workspace>/.vaws-local/coordinator` | Coordinator-owned pool state and machine directory. The launch layer may seed `machines.json` from the shared inventory document. |

The package does not read former checkout-root environment variables.

There is **no default manager `--state-dir`**. Requesting remote execution
without a manager is blocked/unavailable.

## 4. Two MCP providers

| Server | Command | Serves |
|---|---|---|
| `remote-dev` | `.venv/bin/python -m remote_dev.mcp.server` | `remote_*` |
| `vaws-task` | `.venv/bin/python -m vaws_coordinator task-server` | `vaws_session`, `vaws_run`, `vaws_execution`, `vaws_finish` |

Native attach/resume keeps the existing task id. A new native session creates
a distinct task unless the user explicitly associates it. Cwd and recent
activity never join a task.

Configure clients with `python3 .agents/scripts/vaws_client_setup.py`.

## 5. Host NPU queue

`session_gc.py` / `npu_coordination.py` still ship a module path to the remote
host. `.agents/lib/vaws_host_queue_module.py` is a thin shim over
`vaws_coordinator.host_queue`. `host_queue_module_path()` returns
`Path(module.__file__)`.
