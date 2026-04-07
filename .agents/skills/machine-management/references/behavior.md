# Machine-management behavior reference

This file is the detailed contract for the `machine-management` skill.

## Ownership boundary

This skill owns the remote-machine layer for this workspace:

- ensure the local workspace machine profile exists when needed
- add a host to local inventory
- create or adopt one managed workspace container on that host
- verify readiness
- repair bounded host/container drift
- remove the managed container and clean mesh trust

It does **not** own:

- code sync into the container
- replacing `vllm` or `vllm-ascend` source trees
- rebuilding packages or native extensions
- model serving or benchmarking

## Local state contract

Repo-local runtime state lives under `.vaws-local/`.

Rules:

- keep it local and untracked
- read it before mutating remote state
- write it after successful add, identity-changing repair, or remove
- alias and host IP must not resolve to different records
- machine username must be letters and digits only, normalized to lowercase
- v1 supports one managed workspace container per host
- wrapper scripts stream phase progress on `stderr` as `__VAWS_PROGRESS__=<json>` and keep one final JSON result on `stdout`
- on a missing profile, `workspace_profile.py ensure` must use either `--username` or `--generate`

Relevant files:

- `.vaws-local/machine-profile.json`
- `.vaws-local/machine-inventory.json`

Compatibility rule:

- read legacy repo-root `.machine-inventory.json` when the new path is still absent
- migrate to `.vaws-local/machine-inventory.json` on the next successful inventory write

## Public API surface contract

Normal agent-facing machine workflows should go through the task wrappers:

- `machine_add.py`
- `machine_verify.py`
- `machine_repair.py`
- `machine_remove.py`

The low-level helpers remain implementation tools:

- `manage_machine.py`
- `inventory.py`
- `workspace_profile.py`

Rules:

- keep the public wrapper surface narrow and task-oriented
- keep alias compatibility in the parser layer, not in `AGENTS.md` or the main skill narrative
- let wrappers infer safe defaults such as container name, container port reuse, bootstrap method reuse, and removal metadata from profile or inventory whenever possible
- low-level helpers may stay alias-tolerant for robustness, but they are not the normal entrypoint for add / verify / repair / remove

`inventory.py` stores canonical `bootstrap_method` values as:

- `ssh`
- `password-once`

For compatibility, `inventory.py put --bootstrap-method key` normalizes to `ssh`.

CLI ergonomics rules:

- disable argparse prefix abbreviation for helper scripts so mistyped flags fail clearly
- accept common aliases on inventory writes:
  - `--host` = `--host-ip`
  - `--user` = `--host-user`
  - `--machine-username` or `--username` = `--namespace`
  - `--name` = `--container-name`
  - `--container-port` = `--container-ssh-port`
- `inventory.py put` / `upsert` should default `bootstrap_method` to `ssh` for a new record and preserve the stored value when updating unless the caller overrides it explicitly


## Namespace and container naming contract

The local machine profile provides a stable workspace machine username / namespace.

Rules:

- create or reuse the profile before new-machine setup
- accept letters and digits only
- normalize to lowercase
- default/random is valid only after the user explicitly accepts it
- derive new container names from the namespace, for example `vaws-alice123`
- if inventory already records a container name for a managed machine, keep using the recorded name for that machine

## Ready vs not-ready

### `ready`

All of the following are true:

- host key SSH works
- direct container SSH works
- the recorded container exists and matches inventory identity
- the smoke test succeeds inside the container

### `needs_input`

Use this when the request is blocked by missing input or by an auth boundary the skill is not allowed to bypass, for example:

- no local public key exists
- verify-only was requested, but repair would be required
- the user wants a specific machine username but has not chosen one yet
- host key SSH is missing and no approved bootstrap path is available

### `needs_repair`

Use this when the machine is managed but direct readiness checks fail and repair is appropriate, for example:

- host key SSH works but container SSH fails
- container SSH works but smoke fails
- inventory and actual container identity drifted

### `blocked`

Use this when the host or image prerequisites fail, for example:

- Docker is missing or unusable
- required Ascend/NPU devices or mounts are absent
- image pull is needed but fails

## Host auth contract

The only allowed password use is one initial host bootstrap for a new machine.

After host key auth is established:

- stop using the password
- never use a container password
- never use `sshpass` or `expect`
- never persist the password into tracked files or `.vaws-local/`

Preferred helper:

- `manage_machine.py bootstrap-host-key`

Rules for that step:

- if the user already supplied a password in the request, prefer the scripted one-shot path first
- prefer `--password-env` or `--password-stdin` when the tool can hide the secret
- allow `--password` only when the user already exposed the password in the current chat and the tool cannot hide stdin/env
- keep `--print-command` as a fallback, not the default
- interactive terminal prompting is fallback behavior, not the primary path
- avoid `ssh-copy-id` as the primary mechanism because it is not consistently available across platforms

## Container SSH contract

The helper uses a dedicated config file:

- `/etc/ssh/sshd_vaws_config`

Why:

- it avoids brittle inline edits to distro `sshd_config`
- it avoids the `Port 22` collision on host-network containers
- it keeps the managed `sshd` restart path deterministic

The managed config must enforce:

- high non-default port
- root key login
- password auth disabled
- dedicated PID file

The container bootstrap must ensure `/run/sshd` exists before starting the dedicated daemon.
Remote-script arguments that may contain spaces, such as SSH public keys or mesh peer keys, must survive the local -> ssh -> remote-shell hop intact. Do not rely on raw argv joining for those values.


## Smoke contract

The smoke path must remain dynamic and conservative:

- discover Python instead of hard-coding `python3.11.x`
- source only existing env scripts
- preseed `PATH` and `LD_LIBRARY_PATH`
- prefix driver library paths:
  - `/usr/local/Ascend/driver/lib64/driver`
  - `/usr/local/Ascend/driver/lib64`
- do not add toolkit `devlib` by default

The recorded session showed that adding `devlib` advanced past one missing-library error but introduced ABI mismatch. Driver-library prefixing alone was the stable fix.

## Mesh contract

Use stable key comments such as `vaws-mesh:<alias-or-ip>` so later cleanup is deterministic.

Best-effort behavior:

- generate a container-local mesh key if absent
- append peer keys idempotently
- add peer endpoints to container `known_hosts`
- skip unreachable peers without failing the primary request

## Removal contract

Removal must be bounded:

- remove only the inventory-recorded container
- remove the local container endpoint from local `known_hosts`
- best-effort remove mesh trust from peers
- remove the inventory record

Do **not**:

- remove host firewall rules
- remove host-level `authorized_keys`
- guess at unmanaged containers

## Image selection policy

Image selection is an explicit decision gate, not an implicit default:

1. ask the user to choose `main`, `stable`, or a custom image reference before new-machine bootstrap
2. `main` resolves to `quay.nju.edu.cn/ascend/vllm-ascend:main`, then `quay.io/ascend/vllm-ascend:main`
3. `stable` resolves the latest official non-prerelease `vllm-ascend` release tag at execution time, then tries NJU first and `quay.io` second
4. custom references must include a concrete non-`latest` tag or digest; `auto`, `*:latest`, and bare repositories without a tag are forbidden defaults
5. if fresh pulls fail but one of the explicit candidate refs is already cached locally, reuse that cached image as a bounded fallback
6. for non-destructive attach / repair, a recorded explicit non-`latest` image may be reused; ambiguous legacy images require another user choice

Inventory should record the actual selected image, not only the requested selector string.

## Observability and timeout contract

- wrappers and low-level helpers should report phase progress early enough for an agent to distinguish `probe`, `bootstrap`, `smoke`, `inventory`, and `verify` work
- `stderr` carries progress events; `stdout` stays reserved for the final machine-readable JSON payload
- host probe, container bootstrap, and smoke should each have an overall timeout budget in addition to SSH connect timeouts
- long-running bootstrap steps should keep emitting attributable heartbeats for image pull, `apt-get update`, and `apt-get install` instead of going silent behind one generic timeout
- timeout failures should preserve the last known phase and any successfully returned remote payload
