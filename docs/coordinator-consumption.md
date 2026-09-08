# Consuming vaws-coordinator

Status: current

The shared runtime pool, local task registry and four `vaws_*` tools used to
live in this repository under `.agents/coordinator/` and a handful of
`.agents/lib` modules. They now live in
[`vllm-ascend-workspace/vaws-coordinator`](https://github.com/vllm-ascend-workspace/vaws-coordinator)
at accepted main `a7d5005a4df6ab8adf5b16a965127e81a30ee3fc` (tree
`2660b7fe09c660b8827444753a87ec3bf554d551`). This scaffold consumes that
checkout as an **external checkout**, matching the remote-dev pin/install
pattern from #90.

This document is the consumer-side contract. The remote-dev pin in
`.agents/deps/remote-dev.json` is the accepted provider main
`62045af1f76c803ca392ae413b56bcfe290e6450`. This scaffold does not add an
exact-pin execution gate; an explicit developer checkout remains supported.

## 1. Why an external checkout

Same reasons as [remote-dev-consumption.md](remote-dev-consumption.md): a
submodule would couple cloners to a second repository's availability, a
vendored copy would keep a second task-state writer, and an installable
package is not how the coordinator is run (`task_server.py`, `server.py`,
`scripts/vaws.py`). One directory outside the tracked tree, one environment
variable, and a tracked pin.

**Consequence:** cloning this scaffold no longer gives you the task tools or
the HTTP manager. Local docs and Git work are unaffected. Point
`VAWS_COORDINATOR_ROOT` at a checkout, or run `python3 .agents/scripts/vaws.py
bootstrap`. Missing/old/incompatible checkouts return `blocked` /
`unavailable` with that instruction. Nothing falls back to in-tree copies of
the moved writers.

## 2. Locating the checkout

Order, implemented in `.agents/lib/vaws_coordinator.py`:

1. `VAWS_COORDINATOR_ROOT`, if set.
2. `<shared workspace root>/.vaws-local/vaws-coordinator`.

A path that does not contain `task_server.py`, `scripts/vaws.py`,
`hooks/vaws_session.py`, `lib/vaws_ops.py`, `lib/vaws_agent_session.py`,
`lib/vaws_task_client.py`, `workers/managed_jobs.py` and `server.py` is
rejected.

```bash
python3 .agents/scripts/vaws.py bootstrap   # clone/fast-forward the pin
python3 .agents/scripts/vaws.py status      # location, revision, pin drift
python3 .agents/scripts/vaws.py env --json  # the environment it injects
```

The pin lives in [`.agents/deps/coordinator.json`](../.agents/deps/coordinator.json).

## 3. What the scaffold injects

| Variable | Value | Why |
|---|---|---|
| `VAWS_AGENT_SESSIONS_DIR` | `<shared workspace>/.vaws-local/agent-sessions` | One local task registry. Linked worktrees already share this path through `vaws_local_state.agent_sessions_root()`. |
| `VAWS_HOST_QUEUE_MODULE` | unset by this scaffold | Host NPU authority is the coordinator's bundled `host/vaws_npu_coordination.py`. The env var is an override the scaffold no longer sets. |
| `VAWS_MACHINE_INVENTORY` | the shared inventory JSON | Optional alias registration for the HTTP manager. |
| `VAWS_PARITY_SCRIPT` | `remote_code_parity.py` | Parity owns materialization. The coordinator only consumes `sync --apply-mode materialize`. |
| `VAWS_REMOTE_DEV_ROOT` | the remote-dev checkout, when present | Optional. Local attach/finish/session do not need it. |

There is **no default manager `--state-dir`**. The extracted `server.py`
requires the flag. If a pool database already exists at the pre-extraction
path `<shared workspace>/.vaws-local/coordinator`, pass that path to preserve
pool identity. Do not start or reconfigure the manager from this package.

## 4. Two MCP providers

| Server | Launcher | Serves |
|---|---|---|
| `remote-dev` | `.agents/scripts/remote_dev.py server` | `remote_*` |
| `vaws-task` | `.agents/scripts/vaws.py task-server` | `vaws_session`, `vaws_run`, `vaws_execution`, `vaws_finish` |

Native attach/resume keeps the existing task id. A new native session creates
a distinct task unless the user explicitly associates it. Cwd and recent
activity never join a task.

Local attach/finish work with no manager, no remote-dev and no network.
`vaws_run` without a manager returns `isError` / `blocked` / `unavailable`
and records only a planned local execution.

### Client setup preservation

`.agents/scripts/vaws_client_setup.py` previews files and writes only with
`--apply`. Tests use temporary fixtures only.

When setup runs with an explicit `VAWS_COORDINATOR_ROOT` or
`VAWS_AGENT_SESSIONS_DIR`, those absolute paths are copied into the generated
`vaws-task` env and into the native hook command (`--coordinator-root`,
`--agent-sessions-dir`). A later GUI client does not inherit the setup shell.
Existing user-managed provider env keys and unknown fields still win. Re-running
setup replaces the one hook that execs this checkout's `.agents/hooks/vaws_session.py`
for that client and project, including the older generated command without
explicit path flags. A same-basename script in another path or worktree is not
owned. Mixed groups keep unrelated sibling entries, matchers and group metadata.
Default installation leaves `VAWS_COORDINATOR_ROOT` unset so the locator's default
checkout remains in force.

The coordinator's own JSON helper rewrites `command` / `args` / `type` for a
same-name provider. **This scaffold does not apply that helper as a
migration.** JSON merge here keeps those fields when they already exist,
fills missing env keys, preserves unknown fields, and is idempotent. TOML
already preserves named servers and only adds the missing one. Stale
`mcp__remote-dev__vaws_*` permission rules are reported in the plan JSON and
are not rewritten. `--task-only` writes only `vaws-task`.

## 5. What was deleted after arrival evidence

Destination commit `d3c4e82a3c0e3f0be31727abf17b7863bcedba77`, tree
`d3f91bc6375a876fc01d46b1835feabd61db2729`. Blobs are recorded in
`.agents/deps/coordinator.json` `arrival_blobs`.

| Deleted from this tree | Destination path |
|---|---|
| `.agents/coordinator/` | repository root of vaws-coordinator |
| former scaffold lib `vaws_agent_session.py` | `lib/vaws_agent_session.py` |
| former scaffold lib `vaws_task_client.py` | `lib/vaws_task_client.py` |
| former scaffold lib `vaws_ready_runtime.py` | `lib/vaws_ready_runtime.py` |
| former scaffold lib `vaws_managed_execution.py` | `lib/vaws_managed_execution.py` |
| former scaffold lib `vaws_runtime_profile.py` | `lib/vaws_runtime_profile.py` |

Kept as a **byte-pinned mirror** (sha256
`967adeb699e47de2e281581d576a69f6c85075385975e42916ead1ed198a2e09`):
`.agents/lib/vaws_build_inputs.py`. Parity build keys must not diverge from
the coordinator verifier. This is a pure helper, not a second task-state
writer.

Kept as scaffold authorities: `vaws_run_manifest.py`,
`vaws_local_state.py` (including `agent_sessions_root()`). Host NPU
authority is consumed from the pinned checkout
(`host/vaws_npu_coordination.py`); the scaffold locator is
`.agents/lib/vaws_host_queue_module.py`.

## 6. Residual compatibility adapters

| Adapter | Why it remains |
|---|---|
| `.agents/lib/vaws_coordinator.py` | Locator / pin / environment. Does not write task state. |
| `.agents/scripts/vaws.py` | Launcher. Exec's the checkout; fail-closed without it. |
| `.agents/hooks/vaws_session.py` | Historical hook path. Exec's the checkout hook; writes nothing itself. |
| `.agents/scripts/vaws_client_setup.py` | Dual-provider setup with the stronger JSON preservation the coordinator helper does not provide. |
| `.agents/lib/vaws_build_inputs.py` | Byte-pinned parity mirror. |

## 7. Combined-tree sources

This tree is the ordinary merge of the accepted coordinator consumer
`f39f284acbfe4a5cf8cfb06b15a41fba0d64361e` with public scaffold main
`257dc131c2015d0e01445288efb89bc5ab825b5f`. The remote-dev pin is the
accepted provider main `62045af1f76c803ca392ae413b56bcfe290e6450`.

#91's reconciliation ledger may copy destination commit/blob rows from
`.agents/deps/coordinator.json` and this document. Linux subreaper tests
remain Linux CI evidence of the coordinator repository; macOS skips them.
