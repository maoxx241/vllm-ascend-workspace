# Consuming vaws-coordinator

The shared runtime pool, local task registry and four `vaws_*` tools used to
live in this repository under `.agents/coordinator/` and a handful of
`.agents/lib` modules. They now live in
[`vllm-ascend-workspace/vaws-coordinator`](https://github.com/vllm-ascend-workspace/vaws-coordinator)
at accepted main `2e16e894e31a12d85a11117a2772031f30fdfebe` (tree
`fc64eacacf16060446895e2fa0a23a1fe0d17b4e`). This scaffold consumes that
checkout as an **external checkout**, matching the remote-dev pin/install
pattern from #90.

This document is the consumer-side contract. It does **not** claim that
scaffold #90 is merged or that the remote-dev pin in
`.agents/deps/remote-dev.json` is the final post-glob/mux pin. That pin stays
#90-owned.

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
| `VAWS_HOST_QUEUE_MODULE` | `.agents/lib/vaws_npu_coordination.py` | Host NPU authority stays in the scaffold. The coordinator ships the module to the host and speaks `handle_request` / `CoordinationError`. |
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

The coordinator's own JSON helper rewrites `command` / `args` / `type` for a
same-name provider. **This scaffold does not apply that helper as a
migration.** JSON merge here keeps those fields when they already exist,
fills missing env keys, preserves unknown fields, and is idempotent. TOML
already preserves named servers and only adds the missing one. Stale
`mcp__remote-dev__vaws_*` permission rules are reported in the plan JSON and
are not rewritten. `--task-only` writes only `vaws-task`.

## 5. What was deleted after arrival evidence

Destination commit `2e16e894e31a12d85a11117a2772031f30fdfebe`, tree
`fc64eacacf16060446895e2fa0a23a1fe0d17b4e`. Blobs are recorded in
`.agents/deps/coordinator.json` `arrival_blobs`.

| Deleted from this tree | Destination path |
|---|---|
| `.agents/coordinator/` | repository root of vaws-coordinator |
| `.agents/lib/vaws_agent_session.py` | `lib/vaws_agent_session.py` |
| `.agents/lib/vaws_task_client.py` | `lib/vaws_task_client.py` |
| `.agents/lib/vaws_ready_runtime.py` | `lib/vaws_ready_runtime.py` |
| `.agents/lib/vaws_managed_execution.py` | `lib/vaws_managed_execution.py` |
| `.agents/lib/vaws_runtime_profile.py` | `lib/vaws_runtime_profile.py` |

Kept as a **byte-pinned mirror** (sha256
`967adeb699e47de2e281581d576a69f6c85075385975e42916ead1ed198a2e09`):
`.agents/lib/vaws_build_inputs.py`. Parity build keys must not diverge from
the coordinator verifier. This is a pure helper, not a second task-state
writer.

Kept as scaffold authorities: `vaws_npu_coordination.py`,
`vaws_run_manifest.py`, `vaws_local_state.py` (including
`agent_sessions_root()`).

## 6. Residual compatibility adapters

| Adapter | Why it remains |
|---|---|
| `.agents/lib/vaws_coordinator.py` | Locator / pin / environment. Does not write task state. |
| `.agents/scripts/vaws.py` | Launcher. Exec's the checkout; fail-closed without it. |
| `.agents/hooks/vaws_session.py` | Historical hook path. Exec's the checkout hook; writes nothing itself. |
| `.agents/scripts/vaws_client_setup.py` | Dual-provider setup with the stronger JSON preservation the coordinator helper does not provide. |
| `.agents/lib/vaws_build_inputs.py` | Byte-pinned parity mirror. |

## 7. Remaining integration gate

Root's #90 owner still supplies the final accepted scaffold main and the
remote-dev pin after glob/mux integration. This package stacks on #90 exact
`5ad2d9bd67f5f208f1b02a55494d056e7c80abfc` and does not overwrite
`.agents/deps/remote-dev.json`. Provisional local integration may set
`VAWS_REMOTE_DEV_ROOT` at accepted remote-dev main
`2c8e2503359a311721328d70d3f3a16aa65174ae` without changing that pin.

#91's reconciliation ledger is not in this #90 tree. Arrival evidence is in
`.agents/deps/coordinator.json` and this document; the #91 owner can copy
those destination commit/blob rows. Linux subreaper tests remain Linux CI
evidence of the coordinator repository; macOS skips them.
