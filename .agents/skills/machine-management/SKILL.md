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
- Keep local runtime state only under `.vaws-local/`.
- Never write passwords or tokens into tracked files or `.vaws-local/`.
- Never use `scp`, `sftp`, `sshpass`, or `expect` in this workflow.
- Never default the container image silently. Ask the user to choose one of:
  - `rc`: resolve the newest official prerelease `vllm-ascend` tag, then try `quay.nju.edu.cn/ascend/vllm-ascend:<tag>` first and `quay.io/ascend/vllm-ascend:<tag>` second; this is the recommended developer track
  - `main`: `quay.nju.edu.cn/ascend/vllm-ascend:main`, then `quay.io/ascend/vllm-ascend:main`
  - `stable`: resolve the latest official non-prerelease `vllm-ascend` release tag, then try NJU first and `quay.io` second
  - `custom`: a full image reference with a concrete non-`latest` tag or digest
- Treat `auto`, `*:latest`, and bare repositories without a tag as forbidden defaults for managed-machine bootstrap.
- Report and persist the **actual selected image** for the managed container, not only the requested image policy.
- Resolve hardware-specific image tags from the detected machine type whenever the user chose `rc`, `main`, or `stable`: A2 uses the base tag, A3 appends `-a3`, and 310P appends `-310p`.
- Detect the machine type from `npu-smi info` / SoC output when possible; when detection is inconclusive, stop and ask for an explicit machine type override instead of guessing.
- Persist `host.machine_type`, `host.soc`, and `container.machine_type` into inventory, and write matching metadata under `/etc/vaws/` plus `/etc/profile.d/vaws-ascend-env.sh` on the host and inside the managed container.
- Before running `apt-get update` / `apt-get install` inside the container, probe a small set of reachable domestic mirrors, switch to the fastest usable one, and keep the original source host only as a fallback.
- Prepend `/usr/local/Ascend/driver/lib64/common`, `/usr/local/Ascend/driver/lib64/driver`, and `/usr/local/Ascend/driver/lib64` before calling `npu-smi` or the smoke test; source `/etc/profile.d/vaws-ascend-env.sh` when it exists.
- Long probe / bootstrap / smoke operations must expose bounded phase progress and have an overall timeout budget, not only a connect timeout.
- On a missing local machine profile, never call `workspace_profile.py ensure` bare. Use either:
  - `--username <letters-or-digits>` after the user chose a name
  - `--generate` only after the user explicitly accepted the default/random option
- If host key SSH is missing and the user already supplied the host password in the request, prefer one-shot scripted bootstrap first. Do not immediately push the user to a manual terminal command.

## Cross-platform launcher rule

- macOS / Linux / WSL: `python3 ...`
- Windows: `py -3 ...`

The primary bootstrap path must not depend on `ssh-copy-id`, `expect`, or any other POSIX-only interactive tool.

## Public workflow entry points

Use these task-oriented wrappers for normal agent work. They keep the parameter surface narrow and return structured JSON statuses such as `ready`, `needs_input`, `needs_repair`, `blocked`, `removed`, or `unmanaged`. They also stream phase progress on `stderr` as `__VAWS_PROGRESS__=<json>` while reserving `stdout` for one final machine-readable JSON payload.

- `python3 .agents/skills/machine-management/scripts/machine_add.py --host <ip> --image <rc|main|stable|custom-ref> [--machine-type <A2|A3|310P>] [--machine-username <letters-or-digits> | --generate-machine-username] [--password-env NAME | --password-stdin | --password ...]`
- `python3 .agents/skills/machine-management/scripts/machine_verify.py --machine <alias-or-ip>`
- `python3 .agents/skills/machine-management/scripts/machine_repair.py --machine <alias-or-ip> [--image <rc|main|stable|custom-ref>] [--machine-type <A2|A3|310P>] [--password-env NAME | --password-stdin | --password ...]`
- `python3 .agents/skills/machine-management/scripts/machine_remove.py --machine <alias-or-ip>`

Design intent:

- `machine_add.py` owns the full attach path: profile, host auth, probe, container bootstrap, inventory write, and best-effort mesh
- `machine_verify.py` is read-only
- `machine_repair.py` owns conservative repair of an already managed machine
- `machine_remove.py` owns bounded removal and local cleanup

## Internal helpers

These remain available for deterministic implementation work and debugging, but they are **not** the normal agent-facing surface for add / verify / repair / remove:

- `.agents/skills/machine-management/scripts/manage_machine.py`
- `.agents/skills/machine-management/scripts/inventory.py`
- `.agents/scripts/workspace_profile.py`

Do not start with the low-level helpers unless the wrapper lacks a capability the user explicitly needs.

Keep alias compatibility in the parser layer, not in the main skill narrative.

## Local state

Local workspace-machine state lives under `.vaws-local/`:

- `.vaws-local/machine-profile.json`
- `.vaws-local/machine-inventory.json`

Compatibility note:

- the helper still reads legacy repo-root `.machine-inventory.json` when the new path does not exist yet
- the next successful inventory write migrates state to `.vaws-local/machine-inventory.json`

## Workflow

### 1. Normalize the request

Classify the request as one of:

- `add`
- `verify`
- `repair`
- `remove`

Then choose the matching wrapper entrypoint.

### 2. Ensure the local machine profile when needed

For `add` and any first-time attach flow:

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
- whether `npu-smi` / SoC output identifies the host as A2, A3, or 310P
- whether a free high SSH port exists
- whether a managed container already exists

### 4. Host auth boundary

Password policy:

- allowed: one bare-metal password-authenticated bootstrap during the first add of a new machine
- forbidden: repeated server password prompts after the initial bootstrap, any container password prompt, or `sshpass` / `expect`

If host key auth already works, do not use the password even if the user provided one.

If host key auth does not work and the wrapper lacks an approved password source, return `needs_input` instead of trying an interactive prompt.

When password bootstrap is required:

- if the user already supplied a password in the request, prefer scripted bootstrap with `bootstrap-host-key`
- prefer `--password-env` or `--password-stdin` when the tool can hide the value
- `--password` is acceptable only when the user already wrote the password in the current chat and the agent tool cannot hide stdin/env
- keep manual print-command fallback in the low-level helper, not the normal wrapper path

### 5. Add or attach workflow

`machine_add.py` should own this order:

1. ensure or reuse the local machine profile
2. ask for or reuse an explicit image choice; never fall back to `auto` or `latest`
3. if an existing record still points at a legacy or moving image tag, stop for re-selection before any `already-ready` shortcut
4. ensure a local public key exists
5. if needed, establish host key auth
6. probe the host, detect `machine_type` / `soc`, and resolve the hardware-specific image tag
7. choose or reuse the container name and container SSH port
8. bootstrap or repair the managed container, including container-side apt mirror selection before package installation when needed
9. persist the record into inventory
10. best-effort mesh the new container with existing managed containers
11. run final readiness verification

If inventory already contains the same alias or host IP, treat add as an idempotent attach-or-repair path instead of creating a duplicate record.

### 6. Verify workflow

For verify-only requests:

- stay read-only
- use `machine_verify.py`
- do not silently repair drift

### 7. Repair workflow

Prefer non-destructive repair:

- if host key SSH works, use container bootstrap to restart or repair the managed container and its dedicated `sshd`
- once container SSH works again, switch back to direct local -> container SSH
- rerun final readiness verification
- do not recreate or delete a container unless the user explicitly asked for destructive repair

### 8. Remove workflow

Proceed in this order:

1. confirm the machine is managed by this workspace
2. best-effort remove the departing mesh trust from peers
3. remove only the recorded container
4. remove the local endpoint from `known_hosts`
5. remove the machine record from inventory

Do not remove host firewall rules or host-level `authorized_keys` entries.

## Stable implementation notes

- Use the dedicated container SSH config `/etc/ssh/sshd_vaws_config` instead of editing `/etc/ssh/sshd_config` inline.
- Quote remote-script arguments that may contain spaces, especially SSH public keys and mesh peer keys, before sending them through `ssh`.
- Ensure `/run/sshd` exists before starting the dedicated `sshd`.
- Image pulls should follow the selected mirror order and emit heartbeat-style progress so long `docker pull`, `apt-get update`, and `apt-get install` phases remain attributable. Persist the actually selected image in inventory, not only the selector.
- Container bootstrap should leave behind `/etc/vaws/host-info.json`, `/etc/vaws/container-info.json`, and `/etc/profile.d/vaws-ascend-env.sh` so later verify / repair runs can see the recorded machine type, container type, and SoC quickly.
- Container bootstrap should write `/etc/pip.conf` with multiple pip indexes (Tsinghua as primary, Aliyun and PyPI as additional) so any subsequent `pip install` inside the container does not fail due to missing mirror configuration.
- Container bootstrap should install `pytest` into the runtime Python if not already present, so that remote test execution works out of the box without ad-hoc environment setup.
- Keep `rc` and `stable` resolution dynamic: resolve the newest official prerelease tag or latest official non-prerelease release tag at execution time instead of using the moving `latest` tag.
- Keep progress on `stderr` and the final status JSON on `stdout`; do not mix partial human narration into the terminal contract.
- For smoke tests, do not pin a Python patch version. Discover the highest available `/usr/local/python*/bin/python3`, then fall back to `python3`.
- Source only environment scripts that actually exist.
- Preseed `PATH` and `LD_LIBRARY_PATH` before sourcing env scripts under `set -u`.
- Prefix `LD_LIBRARY_PATH` with `/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64`.
- Do not add Ascend `devlib` paths by default.

Reference files:

- `.agents/skills/machine-management/references/behavior.md`
- `.agents/skills/machine-management/references/command-recipes.md`
- `.agents/skills/machine-management/references/acceptance.md`
