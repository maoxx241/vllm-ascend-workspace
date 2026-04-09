# Repository instructions

This repository is a composable scaffold for local `vllm` + `vllm-ascend` development. It is not a mandatory workflow engine.

## Skills available in this repo

Repo-local skills live under `.agents/skills/`.

- `repo-init`: initialize the workspace after clone, including `gh`, GitHub auth, recursive submodules, optional fork / remote topology, and the local workspace machine profile used later by machine-management.
- `machine-management`: add, verify, repair, or remove a workspace-managed remote NPU machine and its managed container.
- `remote-code-parity`: automatically ensure a ready remote runtime uses the exact current local workspace state before any remote smoke, service launch, or benchmark.
- `vllm-ascend-serving`: start, check, or stop a single-node colocated vLLM Ascend online service on a workspace-managed ready remote container.
- `vllm-ascend-benchmark`: run `vllm bench serve` performance benchmarks on a workspace-managed remote container, supporting single-run and A/B comparison modes.

`repo-init` and `machine-management` are optional. `remote-code-parity` is an automatic pre-execution gate only for ready-machine remote execution. `vllm-ascend-serving` handles service lifecycle after parity. `vllm-ascend-benchmark` orchestrates serving + benchmarking end-to-end. Do not force any of them as a gate before normal local coding, docs work, or unrelated Git / SSH tasks.

## Repo-wide operating rules

- Never write secrets, passwords, private keys, tokens, or user-specific machine metadata into tracked files.
- Keep repo-local runtime state only under the untracked directory `.vaws-local/`.
- Keep remote-code-parity state only under `.vaws-local/remote-code-parity/`.
- Keep serving state only under `.vaws-local/serving/`.
- Treat the legacy repo-root `.machine-inventory.json` as compatibility input only.
- Keep `.gitmodules` on community URLs:
  - `https://github.com/vllm-project/vllm.git`
  - `https://github.com/vllm-project/vllm-ascend.git`
- Prefer helper scripts under `.agents/skills/*/scripts/` and `.agents/scripts/` for deterministic work.
- For normal machine-management add / verify / repair / remove work, prefer the task wrappers:
  - `.agents/skills/machine-management/scripts/machine_add.py`
  - `.agents/skills/machine-management/scripts/machine_verify.py`
  - `.agents/skills/machine-management/scripts/machine_repair.py`
  - `.agents/skills/machine-management/scripts/machine_remove.py`
- For normal serving work, prefer the task wrappers:
  - `.agents/skills/vllm-ascend-serving/scripts/serve_start.py`
  - `.agents/skills/vllm-ascend-serving/scripts/serve_status.py`
  - `.agents/skills/vllm-ascend-serving/scripts/serve_stop.py`
  - `.agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py`
- For normal benchmark work, prefer the task wrappers:
  - `.agents/skills/vllm-ascend-benchmark/scripts/bench_run.py`
  - `.agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py`
- Treat `.agents/skills/machine-management/scripts/inventory.py` and `.agents/skills/machine-management/scripts/manage_machine.py` as low-level maintenance helpers, not the default agent-facing surface.
- For machine bootstrap, never default the image silently. Ask the user to choose `rc`, `main`, `stable`, or a concrete custom image reference.
- `rc` is the recommended developer track: resolve the newest official prerelease `vllm-ascend` tag at execution time, then try `quay.nju.edu.cn/ascend/vllm-ascend:<tag>` first and `quay.io/ascend/vllm-ascend:<tag>` second.
- `main` should try `quay.nju.edu.cn/ascend/vllm-ascend:main` first and `quay.io/ascend/vllm-ascend:main` second.
- `stable` should resolve the latest official non-prerelease `vllm-ascend` release tag at execution time, then try NJU first and `quay.io` second.
- Reject `auto`, `*:latest`, and bare repositories without a tag as machine-bootstrap defaults.
- Keep helper CLIs ergonomic: accept common aliases, disable brittle prefix-abbreviation behavior, and default metadata that can be inferred safely.
- For normal remote-code-parity work, prefer `.agents/skills/remote-code-parity/scripts/parity_sync.py` over the low-level `remote_code_parity.py` helper.
- Remote-code-parity should expose phase progress, materialize nested repos explicitly, keep consent/runtime-state writes atomic, unify the runtime Python across `python`, `python3`, CMake, and CANN helper tools, and use mirror-aware pip fallback in the order Tsinghua -> Aliyun -> PyPI.
- Prefer concise machine-readable summaries over long raw command logs.
- When a command is noisy, capture the log and report only a compact summary plus a short failure tail.
- Wrapper-style skills should keep progress on `stderr` and reserve `stdout` for a final machine-readable JSON payload.
- Preserve user choices and extra remotes unless the user explicitly asks to replace them.

## Mandatory decision gates

- Never call `.agents/scripts/workspace_profile.py ensure` on a missing profile without either `--username` or `--generate`.
- During `repo-init`, after the probe and before any mutation, stop for one decision checkpoint that covers:
  - machine username choice for broad init when the profile is missing
  - repo topology choice: keep current, recommended fork mode, or community-only
  - whether to initialize submodules now
- For a missing broad-init machine profile, prefer `.agents/skills/repo-init/scripts/repo_init_profile.py` over calling `workspace_profile.py` directly.
- The broad-init machine-username question should use exactly three options: detected Git username, random `agent#####`, or custom. If the user picks custom, ask one more text question and wait for the literal username before mutating.
- Never treat a custom selection as permission to reuse the detected Git username.
- During new-machine bootstrap, stop for an explicit image choice unless an existing inventory record already points at a concrete non-`latest` image. The named selector tracks are `rc`, `main`, and `stable`; any other choice must be a concrete custom ref.
- During `machine-management`, if host key SSH is missing and the user already supplied the host password in the request, prefer a one-shot scripted bootstrap first. Do not immediately bounce the user to a manual terminal command.
- During `machine-management`, long `docker pull`, `apt-get update`, and package-install phases should keep emitting attributable progress instead of going silent behind one global timeout.

## Mandatory execution gate

- When a ready managed machine is about to run a remote smoke, service launch, or benchmark for the first time, check the sync mode for that container before running `remote-code-parity`:
  - If `sync_mode` is `unset` (first use), proactively ask the user: sync local code (`local`) or use the container's image-provided vllm + vllm-ascend (`image`). Record the choice via `install_consent.py set-sync-mode`.
  - If `sync_mode` is `local`, run `remote-code-parity` normally. Do not continue unless it returned `status == ready`.
  - If `sync_mode` is `image`, skip `remote-code-parity` entirely and proceed with remote execution using image-provided packages.
  - `--force-reinstall` overrides `image` mode and forces a full sync + reinstall.
- If the first runtime replacement is blocked by missing install consent, stop and get consent instead of silently using the image-provided packages.
- The user can switch sync mode at any time by asking to change it.

## Skill routing

Use `repo-init` when the user explicitly asks to:

- initialize the workspace after clone
- install or configure `gh`
- sign into GitHub
- initialize recursive submodules
- configure forks or remotes for the workspace, `vllm`, or `vllm-ascend`
- create or confirm the local workspace machine profile during a broad init

Use `machine-management` when the user explicitly asks to:

- configure or add a remote NPU server for this workspace
- check whether a managed machine is ready
- repair host SSH, container SSH, or managed-container drift
- remove a managed machine from this workspace
- create the local workspace machine profile when repo-init was skipped

Do not use `machine-management` for code sync, source rebuilds, serving, benchmarking, or generic SSH work unrelated to workspace-managed machines.

Use `remote-code-parity` automatically when:

- a ready managed machine is about to run a remote smoke, service launch, or benchmark
- the request depends on local uncommitted changes, untracked files, or other non-ignored local files being reflected remotely
- the container already accepts direct local -> container key-based SSH

Do not use `remote-code-parity` for initial machine attach, SSH repair, generic Git topology work, or unrelated local-only tasks.

Use `vllm-ascend-serving` when the user asks to:

- start, launch, or pull up a vllm-ascend online service on a managed machine
- restart or relaunch a service (possibly with changed flags or env)
- check if a running service is alive or ready
- stop a running service

Do not use `vllm-ascend-serving` for machine attach, environment bootstrap, code sync, benchmark orchestration, or offline inference.

Use `vllm-ascend-benchmark` when the user asks to:

- run a performance benchmark / throughput test on a managed machine
- compare performance before and after a code change (A/B)
- verify there is no performance regression for a PR or commit

Do not use `vllm-ascend-benchmark` for accuracy tests, nightly CI matrix runs, offline inference, or service-only lifecycle without benchmarking.

## Maintenance rule

When you change a skill, update the whole skill package together:

- `SKILL.md`
- `scripts/`
- `references/behavior.md`
- `references/command-recipes.md`
- `references/acceptance.md`

When the change affects shared local-state behavior, also update:

- `.agents/scripts/workspace_profile.py`
- `.agents/lib/vaws_local_state.py`

If you change `remote-code-parity`, update these together:

- `.agents/skills/remote-code-parity/SKILL.md`
- `.agents/skills/remote-code-parity/scripts/`
- `.agents/skills/remote-code-parity/references/`
- `AGENTS.md` and `.agents/README.md` when routing or local-state behavior changes