# Machine-management behavior reference

This file is the detailed contract for the `machine-management` skill.

## Ownership boundary

This skill owns the remote-machine layer for this workspace:

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

`.machine-inventory.json` is the canonical local state file.

Rules:

- keep it local and untracked
- read it before mutating remote state
- write it after successful add, identity-changing repair, or remove
- alias and host IP must not resolve to different records
- v1 supports one managed workspace container per host

`inventory.py` stores canonical `bootstrap_method` values as:

- `ssh`
- `password-once`

For compatibility, `inventory.py put --bootstrap-method key` normalizes to `ssh`.

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
- password bootstrap is needed but no password was provided
- verify-only was requested, but repair would be required

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
- never automate passwords with `sshpass`, `expect`, files, or env scripts

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
