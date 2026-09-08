# Consuming remote-dev

Status: current

The remote development substrate used to live in this repository at
`.remote-dev/`. It is now its own repository,
[`vllm-ascend-workspace/remote-dev`](https://github.com/vllm-ascend-workspace/remote-dev),
and this scaffold consumes it as an **external checkout**. This document is the
consumer-side contract: how the checkout is located, what the scaffold injects
into it, and what changed for anyone who was using former in-tree remote-dev paths.

Phase 1 of the sequenced plan in the dated [2026-09-07 boundary snapshot](audits/repo-boundaries-2026-09-07.md).

## 1. Why an external checkout, and not a submodule or a vendored copy

| Option | Why not |
|---|---|
| **Git submodule** at `.remote-dev/` | The substrate is a **private** repository and this scaffold is public. A third `.gitmodules` entry would make `git clone --recurse-submodules` and `git submodule update --init --recursive` fail for every cloner without access — including the two commands `repo-init` tells agents to run — and it would fail while *initialising the upstream `vllm` / `vllm-ascend` trees they do have access to*. The substrate's own handoff also schedules a history rewrite before it goes public (remote-dev `HANDOFF.md` §6), which changes every commit id, so a gitlink pinned today would dangle. |
| **Vendored copy** | It is what we are removing. A copy drifts, and imports keep succeeding against stale code, which is the failure mode this extraction exists to end. |
| **Installed dependency** (`pip install`) | The substrate ships no packaging metadata and deliberately has no third-party dependencies; it is run as scripts (`mcp/server.py`, `tools/remote_*.py`, `hooks/*.py`). Installing it would add packaging to a repository that does not want it, and would still leave the hooks and the MCP server to be located by path. |
| **External checkout** (chosen) | One directory outside the tracked tree, one environment variable, and a tracked pin. Works for a cloner without access (they get a clear, actionable error instead of a broken `git submodule` run), survives the planned history rewrite (bump `commit` in one JSON file), and keeps `.gitmodules` on the community upstream URLs as `AGENTS.md` requires — in the letter and in the spirit, since no private URL enters it. |

**Consequence for public cloners, stated plainly:** cloning this scaffold no
longer gives you the remote-dev tools. Local work, docs work and every Git task
are unaffected. Remote endpoint work requires access to the private substrate
repository; without it, `remote_dev.py status` reports `state: missing`, the MCP
server refuses to start with that message, and the client hook guards allow
(they are observe-only) rather than blocking the client's own tools. Nothing
silently degrades into "no endpoint" behaviour.

## 2. Locating the checkout

Order, implemented in `.agents/lib/vaws_remote_dev.py`:

1. `VAWS_REMOTE_DEV_ROOT`, if set.
2. `<shared workspace root>/.vaws-local/remote-dev` — one checkout per shared
   workspace, next to the machine inventory, so every linked session worktree
   reaches the same revision.

A path that does not contain `mcp/server.py`, `core/endpoint.py`,
`core/shell_ops.py` and `tools/_cli.py` is rejected as "not a remote-dev
checkout" rather than half-used.

```bash
python3 .agents/scripts/remote_dev.py bootstrap   # clone/fast-forward the pin
python3 .agents/scripts/remote_dev.py status      # location, revision, pin drift
python3 .agents/scripts/remote_dev.py env         # the environment it injects
```

The pin lives in [`.agents/deps/remote-dev.json`](../.agents/deps/remote-dev.json)
(repository, branch, commit). `bootstrap` checks out the pinned commit; `--track-ref`
follows the branch tip instead. `status` reports `pin_matches: false` when the
checkout has drifted. Because the repository is private, `bootstrap` falls back
to `gh repo clone` when a plain `git clone` cannot authenticate.

## 3. What the scaffold injects

The substrate is deliberately ignorant of this scaffold, so everything it needs
is configuration. `.agents/scripts/remote_dev.py` sets it for every substrate
process it starts (MCP server, CLI wrapper, hook), and the client configuration
files repeat the values so they document the contract on their own:

| Variable | Value | Why |
|---|---|---|
| `REMOTE_DEV_RESOLVERS` | `.agents/lib/vaws_remote_dev_plugin.py:setup` | The scaffold's endpoint resolver (section 4). Without it the substrate knows only `host`+`port` and `alias`. |
| `REMOTE_DEV_RUNTIME_ENV_FILE` | `/etc/profile.d/vaws-ascend-env.sh` | The Ascend profile the workspace containers install. The old in-tree copy sourced this path unconditionally; the standalone one sources only what an endpoint names, and **is a no-op when unset**. Unset, remote commands run without CANN/ATB paths and with `/usr/bin/python3` — which looks like a broken container, not a missing configuration value. |
| `REMOTE_DEV_STATE_DIR` | `<repo>/.vaws-local/remote-dev-state` | Job records, read ledgers, logs and artifact manifests, under this repository's own untracked state instead of inside the checkout. |
| `REMOTE_DEV_SSH_MUX_DIR` | `~/.ssh/vaws-mux` | The substrate now defaults to `~/.ssh/remote-dev-mux`; pointing it back at the scaffold's directory keeps one multiplexed SSH connection per endpoint shared with `vaws_ssh` and the managed toolbox. |
| `REMOTE_DEV_DEFAULT_USER` / `_ROOT` / `_CWD` | `root`, `/vllm-workspace`, `/vllm-workspace` | Unchanged permission defaults for MCP sessions. Set by the client configuration, not by the launcher, so a CLI invocation keeps the substrate's own defaults. |

Relative `REMOTE_DEV_RESOLVERS` / `REMOTE_DEV_STATE_DIR` values are made
absolute against the repository root before the substrate sees them: the
substrate resolves them against its process cwd, which is the project root for
`.mcp.json` and `.cursor/mcp.json` but not for every client.

## 4. The resolver contract

`.agents/lib/vaws_remote_dev_plugin.py` registers one resolver, `vaws`, with
`fields = ("machine", "session_id", "session_file")`. It is the scaffold-side
reimplementation of the substrate's deleted `core/endpoint.py::_endpoint_from_managed`:

| Payload | Result |
|---|---|
| `machine` | container SSH endpoint of that inventory record, `kind: managed-machine` |
| `session_id` / `session_file` | container SSH endpoint of that managed session, `kind: managed-session` |
| no selector | the session bound to the nearest worktree (`.vaws-local/current-session.json`, walking upward from cwd and stopping at the repository root) |
| no selector, no binding | `None` — declined, so the substrate emits its own "no endpoint target" error naming the known selectors and resolvers |
| a selector that cannot be resolved | `EndpointError` with the underlying inventory / session message |

Every endpoint it returns carries `runtime_env_file`. Caller fields (`root`,
`cwd`, `user`, `runtime_env`, `identity_file`, `connect_timeout_ms`) are merged
by the substrate on top of the resolver's values, so an explicit `--root` still
wins exactly as it did before.

The module imports nothing from the substrate at import time (a test asserts
this), so the mapping is unit-testable without a checkout, and
`.agents/scripts/remote_dev.py` can report a missing checkout instead of
failing on an import.

## 5. What changed for callers

| Before | Now |
|---|---|
| former in-tree remote-dev `mcp/server.py` | `python3 .agents/scripts/remote_dev.py server` |
| former in-tree remote-dev `tools/remote_bash.py --machine <alias> ...` | `python3 .agents/scripts/remote_dev.py tool remote_bash --machine <alias> ...` (the launcher rewrites `--machine` / `--session-id` / `--session-file` into the substrate's `--selector KEY=VALUE`) |
| former in-tree remote-dev `hooks/claude_remote_guard.py` | `python3 .agents/scripts/remote_dev.py hook claude` (`hook codex` for Codex) |
| former in-tree remote-dev `state/...` | `.vaws-local/remote-dev-state/...` |
| former in-tree remote-dev `tools/sync_claude_skills.py` | `.agents/scripts/sync_claude_skills.py` |
| MCP tool names `remote_*` | unchanged, still served under the `remote-dev` server name |
| MCP tool names `vaws_session` / `vaws_run` / `vaws_execution` / `vaws_finish` | **no longer served here.** They are coordinator semantics and moved to `vllm-ascend-workspace/vaws-coordinator`; see section 6. |

Agent-facing behaviour is otherwise unchanged: a skill that worked with
`--machine` still works with `--machine`, and a zero-argument call inside a
session worktree still auto-binds to that session.

## 6. Coordinator consumption

The task facade and managed-job supervisor belong to `vaws-coordinator`. This
scaffold now consumes that repository the same way it consumes remote-dev: a
tracked pin, an external checkout, and a launcher. See
[coordinator-consumption.md](coordinator-consumption.md).

| Surface | State in this repository |
|---|---|
| `.agents/scripts/vaws.py` | launcher: `status` / `bootstrap` / `env` / `hook` / `task-server` / `attach` / `session` / `run` / `execution` / `finish` |
| `.agents/hooks/vaws_session.py` | compatibility adapter that execs the coordinator hook |
| `.agents/scripts/vaws_client_setup.py` | writes `remote-dev` and `vaws-task`; preserves user-managed servers |
| `.agents/coordinator/` and the moved task-state libraries | deleted after destination commit/blob evidence |

The remote-dev pin stays the accepted provider main in
`.agents/deps/remote-dev.json`; coordinator consumption does not overwrite it.
