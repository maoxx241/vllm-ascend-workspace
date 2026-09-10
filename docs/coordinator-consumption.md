# Consuming vaws-coordinator

Status: current

The local task registry, environment/runtime pool, NPU and port leases, and
the four `vaws_*` tools live in
[`vaws-coordinator`](https://github.com/vllm-ascend-workspace/vaws-coordinator).
This workspace imports that package. It does not clone a checkout, does not
own request association, and does not tick the pool.

See [target-state.md](target-state.md) and [dependency-plane.md](dependency-plane.md).

The closeout candidate is checked with a local editable package. The
published tag and lock update are parent-owned.

## 1. Public actions

Configure clients with `python3 .agents/scripts/vaws_client_setup.py`.
The native hook supplies `context_file`; never guess the task from cwd or
history.

| Tool | Meaning |
|---|---|
| `vaws_session` | Inspect this native attachment's VAWS task; bind actual worktrees |
| `vaws_run` | Submit `command` plus `env` / `environment` / `resources` / `topology` / `timeout_seconds` / `service` / `restart`. Skills do not pass `request_id` / `profile_key` / `runtime_id` / a Python path |
| `vaws_execution` | Status, tail, stop, or read the ordinary endpoint of one owned execution |
| `vaws_finish` | Close admission; stop owned executions; keep container, roots, evidence |

CLI: `python3 .agents/scripts/vaws.py session|run|execution|finish` execs
`python -m vaws_coordinator.vaws`. MCP: `python -m vaws_coordinator task-server`.

A long-running service uses `timeout_seconds=None`, `resources.service_port=0`
(or an explicit port), and a task-scoped business name (`service`). The same
spec reconnects; a changed spec without `restart=True` is an error. `--relaunch`
is `restart=True`. Status reads package facts. Health/first-token are skill
business checks against the returned endpoint and port once the execution is
actually running.

Container hostname `/etc/hosts` repair is coordinator environment preparation,
not a per-model launch snippet.

## 2. Environment this workspace injects

| Variable | Value | Why |
|---|---|---|
| `VAWS_AGENT_SESSIONS_DIR` | `<shared workspace>/.vaws-local/agent-sessions` | One local task registry directory for the package |
| `VAWS_COORDINATOR_STATE_DIR` | unset; package default under `.vaws-local/coordinator` | Coordinator-owned pool and machine directory |
| `VAWS_HOST_QUEUE_MODULE` | unset | Host NPU authority is the package module |

There is no workspace `leases.json` and no `session.json` resource authority.

## 3. User container

Each host has one persistent container `vaws-<user>` (example `vaws-maoxx241`).
Bootstrap, recipe execution, and runtime registration belong to the
coordinator (`python -m vaws_coordinator provision --host ... --image ...
--user ...`). This workspace may store the configured username as project
config; it does not create or delete that container from skills, and the
launcher does not copy project `machine-inventory.json` over coordinator
`machines.json`.

## 4. Public TaskClient

Skills import the installed package. They do not keep a workspace request
ledger or paper over unfinished package behavior.

```python
from vaws_coordinator.task_client import TaskClient

client = TaskClient(context_file)  # native session hook; never cwd/history
client.sources({"vllm": "/actual/vllm", "vllm-ascend": "/actual/vllm-ascend"})
reply = client.run(
    command='"$VAWS_PYTHON" -m vllm.entrypoints.cli.main serve ... --port "$VAWS_SERVICE_PORT"',
    env=None,
    environment={"recipe": "rc", "python_abi": "cp311", "soc": "ascend910b"},
    resources={"npu_count": 2, "service_port": 0},
    timeout_seconds=None,
    service="vllm",
    restart=False,
)
# reply["state"] may be queued | preparing | waiting | waiting_for_runtime | running | ...
observation = client.observe(reply["execution_id"], "status")
target = client.target(reply["execution_id"])  # live only while running
client.observe(reply["execution_id"], "tail")
client.observe(reply["execution_id"], "tail", role="prefill")
client.observe(reply["execution_id"], "target", role="decode")
client.observe(reply["execution_id"], "stop")
client.finish()
```

`preparing` is observable pending state during long environment setup. Skills
report it as `queued` with the same `execution_id`; they do not poll or retry.

A multi-role PD launch uses `topology={"roles": [{"name", "command",
"npu_count"|"devices", "service_port", "host"?, "env"?}, ...]}`. Role `env`
is literal data. The package reserves the full group before any role starts
and returns per-role `target` / `tail` on `observe(..., role=...)` and on
`roles[]`. Unmatched environment constraints stay `waiting_for_runtime`. CLI:
`python3 .agents/scripts/vaws.py session|run|execution|finish`. MCP:
`python -m vaws_coordinator task-server`.

## 5. Source publication to an explicit endpoint

Managed `run` prepares its bound sources. It needs neither a session-management
skill nor a separate parity invocation. Use native Git to inspect local worktrees.

For a prepared direct endpoint outside a managed execution, the optional thin
adapters only construct/call `python -m vaws_coordinator.parity sync` in
`source-only` mode:

```bash
python3 .agents/scripts/remote_sync_plan.py --host <container-host> --port <ssh-port> \
  --runtime-root <prepared-root> --source vllm=<actual-vllm-worktree> \
  --source vllm-ascend=<actual-ascend-worktree>
python3 .agents/scripts/remote_sync_apply.py --host <container-host> --port <ssh-port> \
  --runtime-root <prepared-root> --source vllm=<actual-vllm-worktree> \
  --source vllm-ascend=<actual-ascend-worktree> --dry-run
```

The plan prints the exact package command without remote I/O. Drop `--dry-run`
only for the requested source publication. The adapters refuse an execution ID;
source-only publication does not materialize, install or repair a managed
runtime. Package help is the authority for advanced parity operations.

PD business input contains its service name and complete roles in one config.
`pd_serving.py plan --config ...` no longer creates or consumes a separate
Session Group registry; start still submits one coordinator topology run.
