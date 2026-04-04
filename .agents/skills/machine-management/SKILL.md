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

## Outcome

A successful run leaves the workspace in a predictable local state:

- `.vaws-local/machine-profile.json` records the stable local machine username / namespace.
- `.vaws-local/machine-inventory.json` records the managed machine locally.
- host SSH uses key auth after the initial bootstrap.
- the managed container uses host networking and `/vllm-workspace`.
- the managed container accepts direct SSH as `root@HOST -p CONTAINER_SSH_PORT`.
- the managed container name is collision-safe, derived from the local machine profile, for example `vaws-alice123`.
- the managed container passes the NPU smoke test.
- peer managed containers are mesh-connected when reachable.

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

## Core rules

- Probe first.
- Be idempotent and conservative.
- Keep mutations bounded to the requested machine.
- Treat the bare-metal host as a maintenance plane, not a developer workspace.
- Prefer helper scripts in `scripts/` and `.agents/scripts/` over ad-hoc SSH heredocs.
- Never use `scp`, `sftp`, `sshpass`, or `expect` in this workflow.
- Never write secrets or passwords to disk.
- Never pass host passwords through shell arguments, env vars, or temporary files.

## Local state

Local workspace-machine state lives under `.vaws-local/`:

- `.vaws-local/machine-profile.json`
- `.vaws-local/machine-inventory.json`

Compatibility note:

- the helper still reads legacy repo-root `.machine-inventory.json` when the new path does not exist yet
- the next successful inventory write migrates state to `.vaws-local/machine-inventory.json`

Inspect it first:

```bash
python3 .agents/scripts/workspace_profile.py summary
python3 .agents/skills/machine-management/scripts/inventory.py summary
```

Prefer helper-script writes over hand-editing. Canonical stored `bootstrap_method` values are `ssh` and `password-once`. The inventory helper also accepts `key` as a compatibility alias and normalizes it to `ssh`.

## Script-first entry points

Shared profile helper:

- `python3 .agents/scripts/workspace_profile.py summary`
- `python3 .agents/scripts/workspace_profile.py ensure --username <letters-or-digits>`
- `python3 .agents/scripts/workspace_profile.py ensure`  # auto-generate if missing

Inventory helper:

- `python3 .agents/skills/machine-management/scripts/inventory.py summary`
- `python3 .agents/skills/machine-management/scripts/inventory.py get <alias-or-ip>`
- `python3 .agents/skills/machine-management/scripts/inventory.py put ...`
- `python3 .agents/skills/machine-management/scripts/inventory.py remove <alias-or-ip>`

Remote-machine helper:

- `python3 .agents/skills/machine-management/scripts/manage_machine.py probe-host --host <ip>`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-host-key --host <ip>`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-container --host <ip> --container-name <name> --container-ssh-port <port> --namespace <machine-username>`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py smoke --host <ip> --container-ssh-port <port>`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py verify-machine --host <ip> --container-ssh-port <port>`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py mesh-export-key ...`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py mesh-add-peer ...`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py mesh-remove-peer ...`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py remove-container ...`
- `python3 .agents/skills/machine-management/scripts/manage_machine.py clean-local-known-hosts ...`

Open the reference files only if the helpers are insufficient:

- `.agents/skills/machine-management/references/behavior.md`
- `.agents/skills/machine-management/references/command-recipes.md`
- `.agents/skills/machine-management/references/acceptance.md`

## Readiness definition

A managed machine is ready only when all of the following are true:

- local -> host SSH by key works
- local -> managed-container SSH by key works
- the recorded container exists and matches the inventory identity
- the NPU smoke test succeeds inside the container:
  - `import torch`
  - `import torch_npu`
  - `torch.zeros(1, 2).npu()` succeeds
  - the device reports `npu`

## Workflow

### 1. Normalize the request

Classify the request as one of:

- `add`
- `verify`
- `repair`
- `remove`

Resolve the target by alias or host IP. If an inventory record already exists for the same alias or host IP, pivot from blind add to verify-or-repair instead of creating a duplicate record.

### 2. Ensure the local machine profile

Inspect the local workspace machine profile first.

Rules:

- if a profile already exists, reuse it
- if it is missing and the user is doing machine setup, ask once whether they want a specific machine username
- accept English letters and digits only
- normalize to lowercase
- reject symbols and spaces
- if the user leaves it blank or does not care, auto-generate one with `workspace_profile.py ensure`

Use the resulting machine username as the stable namespace for collision-sensitive identifiers. For new containers, derive the name from that profile, for example `vaws-alice123`.

If inventory already records a container name for the target machine, keep using the recorded name even if the current local profile later changes.

### 3. Probe first

Before any mutation, inspect:

- local machine profile state
- local inventory state
- whether a local public key already exists
- whether host SSH by key already works
- whether Docker and required Ascend/NPU paths exist on the host
- whether a free high SSH port exists
- whether a managed container already exists

Use `workspace_profile.py`, `inventory.py`, and `manage_machine.py probe-host` for these checks.

### 4. Host auth boundary

Password policy:

- allowed: one bare-metal password-authenticated SSH session during the first add of a new machine
- forbidden: repeated server password prompts after the initial bootstrap, any container password prompt, or any password automation

If host key auth already works, do not use the password even if the user provided one.

If host key auth does not work and the request is verify-only, stop with `needs_input` instead of mutating.

When password bootstrap is required:

- prefer `manage_machine.py bootstrap-host-key`
- let the terminal prompt handle the password interactively
- do not echo the password back to the user
- do not pass the password through shell args, env vars, or heredocs
- if the environment cannot surface an interactive prompt, use `bootstrap-host-key --print-command` and ask the user to run that exact foreground command once

### 5. Add or attach workflow

Proceed in this order:

1. ensure a local machine profile exists
2. ensure a local public key exists
3. if needed, do the single interactive host bootstrap with `bootstrap-host-key`
4. run `manage_machine.py probe-host`
5. decide the container name from the local machine profile and choose a free high SSH port
6. run `manage_machine.py bootstrap-container`
7. run `manage_machine.py smoke`
8. persist the record with `inventory.py put`
9. best-effort mesh the new container with existing managed containers

After direct local -> container SSH works, stop using the bare-metal host except when container SSH later breaks.

### 6. Verify workflow

For verify-only requests:

- stay read-only
- prefer `manage_machine.py verify-machine`
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

## Implementation notes to keep stable

- Use the dedicated container SSH config `/etc/ssh/sshd_vaws_config` instead of editing `/etc/ssh/sshd_config` inline.
- For smoke tests, do **not** pin a Python patch version in the instructions. Discover the highest available `/usr/local/python*/bin/python3`, then fall back to `python3`.
- Source only environment scripts that actually exist.
- Preseed `PATH` and `LD_LIBRARY_PATH` before sourcing env scripts under `set -u`.
- Prefix `LD_LIBRARY_PATH` with `/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64`.
- Do not add Ascend `devlib` paths by default; they caused ABI drift in the recorded session.
- New containers should label the namespace so later inspection stays explainable.

## Output discipline

- Prefer helper-script JSON over long raw logs.
- Redirect noisy package-manager output to a temp log and show only a short failure tail.
- Summarize banners and repetitive SSH noise instead of replaying them.
- Keep the final report compact: outcome, evidence, local profile state, inventory state, and remaining manual action.
