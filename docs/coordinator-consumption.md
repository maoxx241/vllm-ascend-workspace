# Consuming vaws-coordinator

Status: current

The local task registry, environment/runtime pool, NPU and port leases, and
the four `vaws_*` tools live in
[`vaws-coordinator`](https://github.com/vllm-ascend-workspace/vaws-coordinator).
This workspace imports that package. It does not clone a checkout, does not
own request association, and does not tick the pool.

See [target-state.md](target-state.md) and [dependency-plane.md](dependency-plane.md).

## 1. Public actions

Configure clients with `python3 .agents/scripts/vaws_client_setup.py`.
The native hook supplies `context_file`; never guess the task from cwd or
history. For local Codex commands, the package can also resolve the actual
`CODEX_THREAD_ID` when the hook did not export `VAWS_CONTEXT_FILE`. Conflicting
native IDs fail explicitly. MCP callers still supply their attachment context.

| Tool | Meaning |
|---|---|
| `vaws_session` | Inspect this native attachment's VAWS task; bind actual worktrees |
| `vaws_run` | Submit `command` plus `env` / `environment` / `resources` / `topology` / `timeout_seconds` / `service` / `restart`. Skills do not pass `request_id` / `profile_key` / `runtime_id` / a Python path |
| `vaws_execution` | Status, tail, stop, or read the ordinary endpoint of one owned execution |
| `vaws_finish` | Close admission; stop owned executions; keep container, roots, evidence |

Task MCP/CLI status may reuse a snapshot for two seconds. Use `refresh: true`
or `python -m vaws_coordinator.vaws execution --refresh` for a new observation. Compact results retain
`observation_freshness` (snapshot completion time, age, source and deferred
refresh) plus per-role sampling times. Busy executions return immediately
with the cached observation and mark refresh as deferred. Python
`TaskClient.observe()` remains fresh by default; `refresh=False` permits caching.
These observations do not grant resource access. Tail, target and stop keep
their existing behavior.

Package CLI: `python -m vaws_coordinator.vaws session|run|execution|finish`.
MCP: `python -m vaws_coordinator task-server`.

A long-running service uses `timeout_seconds=None`, `resources.service_port=0`
(or an explicit port), and a task-scoped business name (`service`). The same
spec reconnects; a changed spec without `restart=True` is an error. `--relaunch`
is `restart=True`. Status reads package facts. Health/first-token are skill
business checks against the returned endpoint and port once the execution is
actually running.
The serving entry follows preparation and readiness within one
`--health-timeout`; `--no-wait` returns the execution receipt immediately.

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
retain that phase and the same `execution_id`. Workflows that need a running
service wait through the owner API instead of resubmitting.

A multi-role PD launch uses `topology={"roles": [{"name", "command",
"npu_count"|"devices", "service_port", "host"?, "env"?}, ...]}`. Role `env`
is literal data. The package reserves the full group before any role starts
and returns per-role `target` / `tail` on `observe(..., role=...)` and on
`roles[]`. Unmatched environment constraints stay `waiting_for_runtime`. CLI:
`python -m vaws_coordinator.vaws session|run|execution|finish`. MCP:
`python -m vaws_coordinator task-server`.

## 5. Source publication to an explicit endpoint

Managed `run` prepares its bound sources. It needs neither a session-management
skill nor a separate parity invocation. Use native Git to inspect local worktrees.

For a prepared direct endpoint outside a managed execution, use the installed
package's `python -m vaws_coordinator.parity sync` API in `source-only` mode.
Package help owns its arguments. This operation publishes source to an explicit
endpoint; it does not allocate, install or repair a managed runtime.

## 6. Owned references and observations

`client.resolve_execution(service="vllm")` resolves within the attached task;
`client.observe(service="vllm")` reads the same authoritative execution. A missing
service returns `not_found`, and multiple live matches require an explicit ID.
Neither lookup allocates devices or starts a service. `client.wait(execution_id,
until="running")` ends at running or a terminal failure; `until="released"`
requires terminal state and confirmed resource release. Timeouts retain the last
observed facts.

Binding a different business worktree automatically returns idle runtime bindings.
Live jobs or unreleased leases still prevent the change. Containers and unrelated
worktrees are preserved.

After successful preflight, coordinator records an immutable `launch_observation`
with source commits, environment profile and environment digest, native build key,
machine, devices and launch command. The target API retains that receipt after
stop; it does not reconstruct it from a subsequently changed binding. Managed
payloads receive it in reserved `VAWS_EXECUTION_OBSERVATION`. It describes the
attested launch and does not claim to detect later runtime mutations.

PD starts directly with `pd_serving.py start --config topology.json`. Its status
and stop operations consume a service or execution reference. Local smoke and
report artifacts record business evidence; coordinator owns topology lifecycle.
