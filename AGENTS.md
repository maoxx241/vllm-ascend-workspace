# Repository instructions

Local `vllm` + `vllm-ascend` development scaffold. `vllm/` and `vllm-ascend/` are Git submodules.

## Skills

Repo-local skills live under `.agents/skills/`. Each has its own `SKILL.md` with usage, entry points, and routing rules — read that before invoking.

| Skill | Purpose |
|-------|---------|
| `repo-init` | Initialize workspace: `gh`, GitHub auth, submodules, fork topology |
| `machine-management` | Add / verify / repair / remove a remote NPU machine |
| `remote-code-parity` | Sync local working tree to remote container before execution |
| `vllm-ascend-serving` | Start / check / stop a vLLM Ascend service on a remote container |
| `vllm-ascend-benchmark` | Run `vllm bench serve` benchmarks (single-run or multi-run with warmup) |

None of these are gates for normal local coding, docs work, or unrelated Git tasks.

## Repo-wide rules

- Never write secrets, passwords, or tokens into tracked files.
- Keep all local runtime state under `.vaws-local/` (untracked).
- Keep `.gitmodules` on community upstream URLs.
- Prefer skill wrapper scripts over raw SSH / shell commands for remote operations.
- Skill wrappers: progress on `stderr`, final JSON on `stdout`.
- This repo targets Huawei Ascend NPU. Local machines (Mac/PC) cannot run `torch`/`torch_npu`-dependent code. Do not attempt local test execution — go straight to the remote container.

## Maintenance

When changing a skill, update the whole package together: `SKILL.md`, `scripts/`, `references/`. When the change affects shared state, also update `.agents/scripts/workspace_profile.py` and `.agents/lib/vaws_local_state.py`.
