# Repository instructions

This repository is a composable scaffold for local `vllm` + `vllm-ascend` development. It is not a mandatory workflow engine.

## Skills available in this repo

Repo-local skills live under `.agents/skills/`.

- `repo-init`: initialize the workspace after clone, including `gh`, GitHub auth, recursive submodules, optional fork / remote topology, and the local workspace machine profile used later by machine-management.
- `machine-management`: add, verify, repair, or remove a workspace-managed remote NPU machine and its managed container.
- `remote-code-parity`: automatically ensure a ready remote runtime uses the exact current local workspace state before any remote smoke, service launch, or benchmark.

`repo-init` and `machine-management` are optional. `remote-code-parity` is an automatic pre-execution gate only for ready-machine remote execution. Do not force any of them as a gate before normal local coding, docs work, or unrelated Git / SSH tasks.

## Repo-wide operating rules

- Never write secrets, passwords, private keys, tokens, or user-specific machine metadata into tracked files.
- Keep repo-local runtime state only under the untracked directory `.vaws-local/`.
- Keep remote-code-parity state only under `.vaws-local/remote-code-parity/`.
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
- Treat `.agents/skills/machine-management/scripts/inventory.py` and `.agents/skills/machine-management/scripts/manage_machine.py` as low-level maintenance helpers, not the default agent-facing surface.
- For machine bootstrap, never default the image silently. Ask the user to choose `main`, `stable`, or a concrete custom image reference.
- `main` should try `quay.nju.edu.cn/ascend/vllm-ascend:main` first and `quay.io/ascend/vllm-ascend:main` second.
- `stable` should resolve the latest official non-prerelease `vllm-ascend` release tag at execution time, then try NJU first and `quay.io` second.
- Reject `auto`, `*:latest`, and bare repositories without a tag as machine-bootstrap defaults.
- Keep helper CLIs ergonomic: accept common aliases, disable brittle prefix-abbreviation behavior, and default metadata that can be inferred safely.
- For normal remote-code-parity work, prefer `.agents/skills/remote-code-parity/scripts/parity_sync.py` over the low-level `remote_code_parity.py` helper.
- Remote-code-parity should expose phase progress, materialize nested repos explicitly, and avoid hard-coding one container Python patch path.
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
- During new-machine bootstrap, stop for an explicit image choice unless an existing inventory record already points at a concrete non-`latest` image. The allowed default tracks are `main` and `stable`; any other choice must be a concrete custom ref.
- During `machine-management`, if host key SSH is missing and the user already supplied the host password in the request, prefer a one-shot scripted bootstrap first. Do not immediately bounce the user to a manual terminal command.
- During `machine-management`, long `docker pull`, `apt-get update`, and package-install phases should keep emitting attributable progress instead of going silent behind one global timeout.


## Mandatory execution gate

- When a ready managed machine is about to run a remote smoke, service launch, or benchmark, run `remote-code-parity` first. Prefer the inventory-driven `parity_sync.py` entrypoint.
- Do not continue remote execution unless `remote-code-parity` returned `status == ready`.
- If the first runtime replacement is blocked by missing consent, stop and get consent instead of silently using the image-provided packages.

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

