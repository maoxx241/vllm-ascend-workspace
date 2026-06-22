# Repository instructions

Local `vllm` + `vllm-ascend` development scaffold. `vllm/` and `vllm-ascend/` are Git submodules.

This repository provides a remote development substrate first, then
vLLM-Ascend skills on top.

## Remote development model

Use native client tools for local files and local shell work.

Use remote companion tools for remote endpoints. The remote tools mirror native
tool semantics and only add endpoint fields:

| Local tool | Remote tool |
|------------|-------------|
| Read | `remote.read` |
| Edit | `remote.edit` |
| Write | `remote.write` |
| Bash | `remote.bash` |
| Glob | `remote.glob` |
| Grep | `remote.grep` |
| LS | `remote.ls` |
| Monitor | `remote.monitor` |
| apply_patch | `remote.apply_patch` |

Default endpoint fields:

- `host`
- `port`
- `user`, default `root`
- `root`, default `/`
- `cwd`, default `/vllm-workspace`

Prefer `host + port` direct endpoints for ordinary remote development.
`session_id`, `session_file`, and `machine` remain advanced compatibility
paths for managed VAWS sessions and legacy single-tenant flows.

Prefer remote companion tools for ordinary remote development. Hooks are
permissive by default, and direct endpoints default to full remote-path
permission (`root=/`). Pass a narrower `root` explicitly when a task requires
path isolation.

## Skills

Repo-local skills live under `.agents/skills/`. Each has its own `SKILL.md` with usage, entry points, and routing rules — read that before invoking.

| Skill | Purpose |
|-------|---------|
| `repo-init` | Initialize workspace: `gh`, GitHub auth, submodules, fork topology |
| `machine-management` | Add / verify / repair / remove a remote NPU machine |
| `session-management` | Create / inspect / remove isolated agent sessions (local worktree + remote container + leases) |
| `remote-toolbox` | Compatibility backend for managed VAWS target/probe/exec/job/sync/service/artifact/cleanup tools |
| `remote-code-parity` | Sync local working tree to remote container before execution |
| `modelscope` | Download / resume / status-check / SHA256-verify ModelScope model weights under explicit local directories |
| `vllm-ascend-serving` | Start / check / stop a vLLM Ascend service on a remote container |
| `vllm-ascend-benchmark` | Run `vllm bench serve` benchmarks (single-run or multi-run with warmup) |
| `ascend-memory-profiling` | Profile HBM memory usage on Ascend NPU for vLLM serving scenarios |
| `ascend-profiling-collection` | Collect one Ascend torch-profiler case end-to-end (start service, bracket workload with `/start_profile` + `/stop_profile`, run `analyse()`, verify outputs, write manifest) |
| `ascend-profiling-analysis` | Analyze collected Ascend torch-profiler roots/manifests and generate reports |

None of these are gates for normal local coding, docs work, or unrelated Git tasks.
For remote endpoint work, prefer `.remote-dev` tools first and use these skills
for domain workflows.

## Repo-wide rules

- Never write secrets, passwords, or tokens into tracked files.
- Keep VAWS runtime state under `.vaws-local/` and remote-dev endpoint/tool
  state under `.remote-dev/state/`. Both are untracked.
- Keep `.gitmodules` on community upstream URLs.
- Prefer `.remote-dev` remote companion tools or skill wrapper scripts over raw SSH / shell commands for remote operations.
- Skill wrappers: progress on `stderr`, final JSON on `stdout`.
- Use the remote-dev substrate for agent-facing remote read/edit/bash/search/patch/job/artifact work. Use the remote toolbox entrypoints as the managed VAWS compatibility backend before falling back to bare SSH.
- For parallel managed remote work, create or reuse a `session-management` session and pass `--session-id` through parity, serving, benchmark, and profiling commands. Legacy `--machine` flows remain available for explicitly single-tenant work.
- This repo targets Huawei Ascend NPU. Local machines (Mac/PC) cannot run `torch`/`torch_npu`-dependent code. Do not attempt local test execution — go straight to the remote container.

## Maintenance

When changing a skill, update the whole package together: `SKILL.md`, `scripts/`, `references/`, `agents/`, and other supporting files as applicable. When the change affects shared state, also update `.agents/scripts/workspace_profile.py`, `.agents/lib/vaws_local_state.py`, `.agents/lib/vaws_session_id.py`, `.agents/lib/vaws_session_state.py`, and `.agents/lib/vaws_remote_toolbox.py` as applicable.
