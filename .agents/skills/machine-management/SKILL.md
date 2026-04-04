---
name: machine-management
description: Add, verify, repair, or remove a managed remote NPU host for this workspace. Use for requests like “配置服务器”, “加一台机器”, “检查 ready”, “修容器 SSH”, or “移除机器”. Do not use for code sync, rebuilds, serving, or benchmarking.
---

# Machine Management

Manage the remote-machine layer for `vllm-ascend-workspace`.

A machine is **ready** only when the managed container:

- accepts direct local -> container SSH by key, and
- passes the container-side `torch` + `torch_npu` smoke test.

Ready does **not** imply code sync, rebuild, serving, or benchmark readiness.

## Use this skill when

- the user asks to add or configure a remote NPU machine
- the user asks whether a managed machine is ready
- the user asks to repair host SSH, container SSH, or managed-container drift
- the user asks to remove a managed machine
- repo-init was skipped and the local machine profile is still missing

## Do not use this skill when

- the task is code sync into the remote container
- the task is replacing `vllm` or `vllm-ascend` source trees
- the task is rebuilding Python packages or native extensions
- the task is serving, benchmarking, or unrelated SSH work

## Critical rules

- Probe first.
- Be idempotent and conservative.
- Keep mutations bounded to the requested machine.
- Treat the bare-metal host as a maintenance plane, not a developer workspace.
- Prefer helper scripts in `scripts/` and `.agents/scripts/` over ad-hoc SSH heredocs.
- Never use `scp`, `sftp`, `sshpass`, or `expect` in this workflow.
- Never write passwords or tokens into tracked files or `.vaws-local/`.
- On a missing local machine profile, never call `workspace_profile.py ensure` bare. Use either:
  - `--username <letters-or-digits>` after the user chose a name
  - `--generate` only after the user explicitly accepted the default/random option
- If host key SSH is missing and the user already supplied the host password in the request, prefer one-shot scripted bootstrap first. Do not immediately push the user to a manual terminal command.

## Cross-platform launcher rule

- macOS / Linux / WSL: `python3 ...`
- Windows: `py -3 ...`

The primary bootstrap path must not depend on `ssh-copy-id`, `expect`, or any other POSIX-only interactive tool.

## Local state

Local workspace-machine state lives under `.vaws-local/`:

- `.vaws-local/machine-profile.json`
- `.vaws-local/machine-inventory.json`

Compatibility note:

- the helper still reads legacy repo-root `.machine-inventory.json` when the new path does not exist yet
- the next successful inventory write migrates state to `.vaws-local/machine-inventory.json`

## Script-first entry points

Shared profile helper:

- `python3 .agents/scripts/workspace_profile.py summary`
- `python3 .agents/scripts/workspace_profile.py ensure --username <letters-or-digits>`
- `python3 .agents/scripts/workspace_profile.py ensure --generate`

Inventory helper:

- `python3 .agents/skills/machine-management/scripts/inventory.py summary`
- `python3 .agents/skills/machine-management/scripts/inventory.py get <alias-or-ip>`
- `python3 .agents/skills/machine-management/scripts/inventory.py put ...`
- `python3 .agents/skills/machine-management/scripts/inventory.py remove <alias-or-ip>`

Remote-machine helper:

- `python3 .agents/skills/machine-management/scripts/manage_machine.py probe-host --host <ip>`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-host-key --host <ip> [--password-env NAME | --password-stdin | --password ...]`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-container --host <ip> --container-name <name> --container-ssh-port <port> --namespace <machine-username>`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py smoke --host <ip> --container-ssh-port <port>`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py verify-machine --host <ip> --container-ssh-port <port>`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py mesh-export-key ...`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py mesh-add-peer ...`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py mesh-remove-peer ...`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py remove-container ...`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py clean-local-known-hosts ...`

Reference files:

- `.agents/skills/machine-management/references/behavior.md`
- `.agents/skills/machine-management/references/command-recipes.md`
- `.agents/skills/machine-management/references/acceptance.md`

## Workflow

### 1. Normalize the request

Classify the request as one of:

- `add`
- `verify`
- `repair`
- `remove`

If inventory already contains the same alias or host IP, pivot from blind add to verify-or-repair instead of creating a duplicate record.

### 2. Ensure the local machine profile

Inspect `.vaws-local/machine-profile.json` first.

Rules:

- if the profile exists, reuse it
- if it is missing, ask once for the machine username
- allowed: English letters and digits only
- normalize to lowercase
- reject spaces and symbols
- default/random is allowed only after the user explicitly accepts it

Use the resulting machine username as the stable namespace for collision-sensitive identifiers. For new containers, derive the name from that profile, for example `vaws-alice123`.

If inventory already records a container name for the target machine, keep using that recorded name even if the current local profile later changes.

### 3. Probe first

Before any mutation, inspect:

- local machine profile state
- local inventory state
- whether a local public key already exists
- whether host SSH by key already works
- whether Docker and required Ascend/NPU paths exist on the host
- whether a free high SSH port exists
- whether a managed container already exists

### 4. Host auth boundary

Password policy:

- allowed: one bare-metal password-authenticated bootstrap during the first add of a new machine
- forbidden: repeated server password prompts after the initial bootstrap, any container password prompt, or `sshpass` / `expect`

If host key auth already works, do not use the password even if the user provided one.

If host key auth does not work and the request is verify-only, stop with `needs_input` instead of mutating.

When password bootstrap is required:

- if the user already supplied a password in the request, prefer scripted bootstrap with `bootstrap-host-key`
- prefer `--password-env` or `--password-stdin` when the tool can hide the value
- `--password` is acceptable only when the user already wrote the password in the current chat and the agent tool cannot hide stdin/env
- keep `--print-command` as a fallback, not the default
- use an interactive terminal prompt only when the user prefers it or automation is unavailable

### 5. Add or attach workflow

Proceed in this order:

1. ensure a local machine profile exists
2. ensure a local public key exists
3. if needed, establish host key auth with `bootstrap-host-key`
4. run `probe-host`
5. decide the container name from the local machine profile and choose a free high SSH port
6. run `bootstrap-container`
7. run `smoke`
8. persist the record with `inventory.py put`
9. best-effort mesh the new container with existing managed containers

### 6. Verify workflow

For verify-only requests:

- stay read-only
- prefer `verify-machine`
- do not silently repair drift

### 7. Repair workflow

Prefer non-destructive repair:

- if host key SSH works, use `bootstrap-container` to restart or repair the managed container and its dedicated `sshd`
- once container SSH works again, switch back to direct local -> container SSH
- rerun `smoke`
- do not recreate or delete a container unless the user explicitly asked for destructive repair

### 8. Remove workflow

Proceed in this order:

1. confirm the machine is managed by this workspace
2. remove only the recorded container
3. remove the local endpoint from `known_hosts`
4. best-effort remove the departing mesh key and endpoint from peer managed containers
5. remove the machine record from inventory

Do not remove host firewall rules or host-level `authorized_keys` entries.

## Stable implementation notes

- Use the dedicated container SSH config `/etc/ssh/sshd_vaws_config` instead of editing `/etc/ssh/sshd_config` inline.
- Ensure `/run/sshd` exists before starting the dedicated `sshd`.
- For smoke tests, do not pin a Python patch version. Discover the highest available `/usr/local/python*/bin/python3`, then fall back to `python3`.
- Source only environment scripts that actually exist.
- Preseed `PATH` and `LD_LIBRARY_PATH` before sourcing env scripts under `set -u`.
- Prefix `LD_LIBRARY_PATH` with `/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64`.
- Do not add Ascend `devlib` paths by default.
