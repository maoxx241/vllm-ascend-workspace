# Repository instructions

This repository is a composable scaffold for local `vllm` + `vllm-ascend` development. It is not a mandatory workflow engine.

## Skills

- Repo-local skills live under `.agents/skills/`.
- This package currently bundles two repo-local skills:
  - `repo-init`
  - `machine-management`
- Both skills are optional. Do not force them as a gate before ordinary exploration, coding, docs work, serving, benchmarking, or unrelated Git tasks.

### Use `repo-init` when

- the user explicitly asks to initialize the repository after clone
- the user asks to install or configure `gh`
- the user asks to sign into GitHub
- the user asks to initialize recursive submodules
- the user asks to configure forks or remotes for the workspace, `vllm`, or `vllm-ascend`
- missing GitHub auth, missing recursive submodules, or broken remote topology is the clear blocker for the requested work

Do not use `repo-init` for unrelated Git operations, normal feature work, or runtime tasks.

### Use `machine-management` when

- the user asks to add or configure a remote NPU server for this workspace
- the user asks to verify whether a managed server or its managed container is ready
- the user asks to repair host SSH, container SSH, firewall exposure, or mesh trust for a managed server
- the user asks to remove a managed server from the local inventory and delete the container created by this workflow
- remote machine readiness is the clear blocker for later runtime work

Do not use `machine-management` for code sync, source rebuilds, serving, benchmarking, or generic SSH work that is unrelated to managed workspace machines.

## Operating rules for `repo-init`

- Stay idempotent and conservative.
- Ask before any environment-changing action:
  - installing tools
  - authenticating `gh`
  - generating or uploading SSH keys
  - forking repositories
  - renaming, deleting, or replacing remotes
  - syncing forks
  - moving branches
  - hard resetting branches
- Allow the user to decline any step without failing the whole run.
- Preserve extra remotes such as `upstream2`.
- Never store user-specific remotes, SSH private keys, personal access tokens, or host-specific secrets in tracked files.
- Prefer SSH for GitHub Git operations when possible.
- Support headless GitHub auth flows.
- If local privilege is missing, hand the user the prepared fallback script and command instead of trying to force a system install.

## Operating rules for `machine-management`

- Keep remote machine state in the repo-root local inventory file `.machine-inventory.json`.
- Treat the inventory as local, untracked workspace state. Do not copy host IPs, ports, passwords, container ports, or other private machine metadata into tracked files outside the skill package.
- v1 manages at most one workspace container per host.
- Default host login is `root@HOST:22` unless the user says otherwise.
- Default image is `quay.nju.edu.cn/ascend/vllm-ascend:latest` unless the user explicitly asks for another tag or digest.
- Default container workdir is `/vllm-workspace`.
- Prefer a container name derived from the developer identity, such as `vaws-<github-login>` when available.
- Choose an unused high SSH port for the container from a non-common range such as `46000-46999`.
- Password use is allowed only during the first bare-metal bootstrap for a newly added server. After that, use SSH key auth only.
- Never write passwords to disk, shell history, tracked files, or helper scripts.
- Do not use `scp`, `sftp`, `sshpass`, or `expect` in this workflow.
- Do not use the bare-metal host for normal development after the managed container is reachable by SSH. Host access is a maintenance plane only.
- Only delete containers that are recorded in `.machine-inventory.json` as skill-managed containers for this workspace. Never delete someone else’s container.
- Removing a server should clean the container endpoint from local `known_hosts` and from peer managed containers’ `known_hosts` and `authorized_keys`, but should not tear down host-level trust or firewall rules.

## Repository model

- `.gitmodules` must keep community URLs on `main`:
  - `https://github.com/vllm-project/vllm.git`
  - `https://github.com/vllm-project/vllm-ascend.git`
- Personal forks are local runtime state, not tracked state.
- `vllm-ascend` personal forks are recommended for PR work.
- `vllm` personal forks are optional.
- workspace personal forks are optional.
- The skill should optimize for the developer-visible repository state, not for preserving a detached submodule checkout forever.

## Validation

When you change the skill packages, review:
- `.agents/skills/repo-init/SKILL.md`
- `.agents/skills/repo-init/references/behavior.md`
- `.agents/skills/repo-init/references/acceptance.md`
- `.agents/skills/machine-management/SKILL.md`
- `.agents/skills/machine-management/references/behavior.md`
- `.agents/skills/machine-management/references/acceptance.md`

