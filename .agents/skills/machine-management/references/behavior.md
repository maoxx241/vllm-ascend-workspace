# Machine-management behavior reference

This file is the detailed contract for the `machine-management` skill.

## Core contract

`machine-management` owns the remote-machine layer for this workspace:

- add a host to the local workspace inventory
- create or adopt exactly one managed workspace container on that host
- verify readiness of the managed container
- repair bounded host/container drift
- remove the managed container and clean mesh trust

It does **not** own:

- code sync into the container
- replacing `vllm` or `vllm-ascend` source trees
- rebuilding Python packages or native extensions
- model serving lifecycle
- benchmarking lifecycle

## Local state contract

The repo-root file `.machine-inventory.json` is the canonical local state file for managed machines.

Rules:

- Keep it local and untracked.
- Read it before mutating remote state.
- Write it after successful add, successful repair that changes identity metadata, and successful remove.
- Alias and host IP must not resolve to different records.
- v1 supports one managed workspace container per host.

Preferred helper:

```bash
python3 .agents/skills/machine-management/scripts/inventory.py summary
```

## Ready vs. not-ready contract

### `ready`

All of the following are true:

- host key SSH works
- direct container SSH works
- the recorded container exists and matches the inventory identity
- pinned Python exists in the container
- required Ascend environment setup exists in the container
- NPU smoke test succeeds inside the container

### `needs_input`

Use this when the request is blocked by missing user-provided input or by an auth boundary that the skill is not allowed to bypass, for example:

- no local public key exists
- password bootstrap is needed but no password was provided
- the runtime cannot safely perform the one allowed interactive password SSH bootstrap
- destructive container recreation would be required but the user did not ask for it

### `needs_repair`

Use this when a managed record exists but one or more fixable layers drifted:

- host key SSH drifted
- container SSH drifted
- firewall no longer exposes the recorded port
- mesh trust drifted
- smoke test fails in an otherwise reachable container

### `blocked`

Use this when prerequisites are missing or unreachable:

- host unreachable
- Docker missing
- required NPU device nodes missing
- required Ascend mounts missing
- no usable image mirror and image absent locally
- no free high container SSH port available

### `removed`

Use this when the inventory record is removed and the managed container is deleted or already absent.

## Stage model

### Stage 0: resolve intent

Classify the request as add / verify / repair / remove.

- “configure/add this server” -> add
- “is this server ready?” -> verify
- “fix this managed machine” -> repair
- “remove/delete this server” -> remove

### Stage 1: inspect inventory and local key state

Required local checks:

- inventory summary
- target record lookup by alias or host IP
- local public key presence
- local `known_hosts` entries for the target endpoint when relevant

### Stage 2: host transport probe

Probe host SSH by key first.

Rules:

- If key auth works, use it.
- If key auth fails during add and a password is available, one interactive password SSH bootstrap is allowed.
- If key auth fails during verify, do not mutate.
- If a password is wrong, stop instead of retrying blindly.

### Stage 3: host prerequisite probe

Required checks before container creation:

- Docker exists and can run containers.
- required devices exist:
  - `/dev/davinci_manager`
  - `/dev/hisi_hdc`
  - `/dev/devmm_svm`
- required host mounts exist:
  - `/usr/local/Ascend/driver`
  - `/usr/local/dcmi`
  - `/usr/local/bin/npu-smi`
  - `/usr/local/sbin`
  - `/usr/share/zoneinfo/Asia/Shanghai`
- chosen image is available locally or pullable from `quay.nju.edu.cn/ascend/vllm-ascend`

If Docker is missing, the skill must not attempt host package-manager installs in v1.

## Container contract

### Required runtime recipe

The managed container must use:

- `--network host`
- `--privileged=true`
- `--shm-size=500g`
- workdir `/vllm-workspace`
- entrypoint `bash`
- the required NPU device nodes
- the required Ascend/system mounts
- the `Asia/Shanghai` timezone bind mount

### Data mounts

The default candidate bind mounts are:

- `/home`
- `/tmp`
- `/weight`
- `/data`
- `/mnt`

Rules:

- include the candidate path when it exists on the host
- use `df -h` when you need to discover additional large shared mount points
- do not fail only because one optional data mount is absent

### Proxy pass-through

Pass proxy env vars through to the container only when:

- the host already has proxy env vars set, and
- the proxy is demonstrably useful

Preferred proxy decision rule:

1. with proxy vars disabled, `curl` to Baidu should still work and `curl` to Google may fail
2. with proxy vars enabled, `curl` to Google should succeed
3. if the proxy does not improve outbound reachability, do not pass it through

## Container SSH contract

The managed container must expose SSH on a non-default high port and support root key login.

Rules:

- choose an unused port from `46000-46999`
- disable password auth inside the container
- permit root login by key
- install both `openssh-server` and `openssh-client`
- start `sshd` explicitly; do not assume systemd exists inside the container
- once direct container SSH works, prefer local -> container SSH over host + `docker exec`

## Python and env contract

Use the preinstalled container Python. Do not install a new Python runtime.

Expected container environment:

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
source /vllm-workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash
export PATH=/usr/local/python3.11.14/bin:$PATH
export PYTHON=/usr/local/python3.11.14/bin/python3
export PIP=/usr/local/python3.11.14/bin/pip
export VLLM_WORKER_MULTIPROC_METHOD=spawn
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
```

Readiness depends on this environment plus the NPU smoke test.

## Mesh contract

Managed containers should trust each other by key when connectivity permits.

Rules:

- generate a container-local mesh key only inside the managed container when needed
- use a stable comment like `vaws-mesh:<alias>` on the container public key
- add that key to peer managed containers’ `authorized_keys`
- add peer endpoints to managed containers’ `known_hosts`
- skip peers that are not reachable instead of failing the primary request
- on removal, delete only the departing mesh key lines and the departing container endpoint records

Never touch host-level trust as part of mesh maintenance.

## Remove contract

A remove request has two distinct success paths:

1. recorded container exists -> delete the recorded managed container, clean local and peer container trust, remove inventory record
2. recorded container already absent -> treat as drift cleanup, clean local and peer container trust if possible, remove inventory record

The skill must not:

- guess at unmanaged containers
- delete containers that are not recorded as skill-managed
- remove host firewall rules in v1
- remove host `authorized_keys` entries

## Stop conditions

Stop and report immediately when:

- auth boundaries are violated
- the request would need destructive recreation that the user did not ask for
- the host is unreachable
- Docker is missing
- the image mirror is unreachable and the image is not already present
- a local state conflict maps one alias to one record and the same host IP to another record

## Cross-skill boundary

- `repo-init` owns local repository bootstrap.
- `machine-management` owns remote machine attach / verify / repair / remove.
- A future code-sync or build skill should own source replacement and package rebuilds.
- A future serving skill should own service lifecycle after a machine is ready.
- A future benchmark skill should own benchmark runs after a service is ready.
