---
name: machine-management
description: Manage remote NPU machines for this workspace. Use when the user asks to add, verify, repair, or remove a server and its managed vllm-ascend container. Do not use for code sync, rebuilds, serving, benchmarking, or generic SSH work.
---

# Machine Management

Use this skill to attach, verify, repair, and remove remote NPU machines for `vllm-ascend-workspace`.

A machine is **ready** only when the managed container:

- accepts direct SSH from the local machine by key, and
- passes the container-side NPU smoke test with the pinned Python and Ascend environment.

Machine ready does **not** imply code sync, source rebuild, serving readiness, or benchmark readiness.

## Outcome

A successful run leaves the workspace in a predictable local state:

- the repo-root local inventory file `.machine-inventory.json` describes each managed machine
- each host uses SSH key auth after the initial bootstrap
- each managed container uses host networking, key-only SSH, and `/vllm-workspace` as the working directory
- the managed container can be reached directly as `root@HOST -p CONTAINER_SSH_PORT`
- the managed container passes the NPU smoke test with `torch` + `torch_npu`
- managed containers are mesh-connected to each other when host-to-host connectivity permits it

## Principles

- Be **probe-first**.
- Be **idempotent**.
- Keep mutations **bounded** to the requested machine.
- Treat the bare-metal host as a **maintenance plane**, not a developer workspace.
- Prefer **local -> container direct SSH** once container SSH is healthy.
- Keep private machine metadata in local inventory only.
- Never write secrets or passwords to disk.
- Never use `scp`, `sftp`, `sshpass`, or `expect` in this workflow.

## When to use this skill

Use it when the user asks to:

- configure a remote server for this workspace
- add a new NPU server
- verify whether a managed machine is ready
- repair host SSH, container SSH, or managed-container drift
- remove a managed server

Typical trigger phrases include:

- “帮我配置一下 125.173.1.2 服务器，密码是 1234”
- “帮我把 125.173.1.3 服务器也配置一下，密码和上一台一样”
- “帮我配置一下 125.173.1.4 服务器，我已经把公钥加进去了你可以直接登录”
- “帮我加一下 125.173.1.5 服务器”
- “帮我移除 125.173.1.6 服务器”

## When not to use this skill

Do not use it for:

- syncing source code to the remote machine or container
- replacing `vllm` or `vllm-ascend` source trees inside the container
- rebuilding `vllm` or `vllm-ascend`
- serving workflows
- benchmarking workflows
- generic SSH requests that are not about managed workspace machines

If one of those later tasks is blocked because the machine is not ready, use this skill only to restore machine readiness, then stop.

## Inputs and defaults

Unless the user says otherwise, use these defaults:

- host user: `root`
- host SSH port: `22`
- image: `quay.nju.edu.cn/ascend/vllm-ascend:latest`
- container workdir: `/vllm-workspace`
- container network: `--network host`
- container SSH port range: choose an unused port from `46000-46999`
- inventory identifier: use the provided alias, else default alias = host IP

Container naming rule:

- prefer `vaws-<github-login>` when a local GitHub login is already discoverable
- otherwise prefer `vaws-<local-username>`
- if the default name collides with a non-managed container, append a short suffix and record the final name in inventory

v1 manages **one workspace container per host**.

## Local inventory

Keep local machine state in the repo-root file `.machine-inventory.json`.

Inspect it first with:

```bash
python3 .agents/skills/machine-management/scripts/inventory.py summary
```

Update it only through the helper script when possible.

Do not copy inventory contents into tracked files.

## Supporting files

Open these only when you need more detail:

- `.agents/skills/machine-management/references/behavior.md`
- `.agents/skills/machine-management/references/command-recipes.md`
- `.agents/skills/machine-management/references/acceptance.md`

Inventory helper:

- `python3 .agents/skills/machine-management/scripts/inventory.py summary`
- `python3 .agents/skills/machine-management/scripts/inventory.py get <alias-or-ip>`

## Readiness definition

A managed machine is ready only when all of the following are true:

- local -> host SSH by key works
- local -> managed-container SSH by key works on the recorded container port
- the managed container provides the expected pinned Python and environment setup
- the NPU smoke test succeeds inside the container:
  - `import torch`
  - `import torch_npu`
  - `torch.zeros(1, 2).npu()` returns successfully
  - the tensor device reports `npu`

## Execution workflow

Follow the stages in order. Keep verify-only requests read-only.

### 1. Normalize the request

Classify the request as one of:

- `add`
- `verify`
- `repair`
- `remove`

Resolve the target by alias or host IP.

Rules:

- if the user asks to configure or add a machine, treat that as `add`
- if the user asks whether a machine is ready, treat that as `verify`
- if the user asks to fix a broken managed machine, treat that as `repair`
- if the user asks to delete or remove a machine, treat that as `remove`

If an inventory record already exists for the same alias or host IP, pivot from blind add to verify-or-repair instead of creating a duplicate record.

### 2. Probe first

Before any mutation, inspect:

- local inventory state
- whether a local public key already exists
- whether host SSH by key already works
- if key auth does not work, whether this is the one allowed first-time password bootstrap
- whether Docker exists on the host
- whether required device nodes and Ascend mounts exist on the host
- whether the mirror image is reachable or already present
- whether a free high SSH port exists
- whether a managed container record already exists and whether the actual container exists

Do not assume the host needs bootstrap before running these probes.

### 3. Host auth boundary

Password policy:

- Allowed: one bare-metal password-authenticated SSH session during the first add of a new machine.
- Forbidden: repeated server password prompts after the initial bootstrap, any container password prompt, or any password automation via `sshpass`, `expect`, files, or env scripts.

If host key auth already works, do not use the password even if the user provided one.

If host key auth does not work and the request is verify-only, stop with `needs_input` instead of mutating.

If the environment cannot perform a one-off interactive password SSH session safely, stop with `needs_input` instead of inventing another workaround.

### 4. Add or attach workflow

For `add` requests, proceed in this order:

1. Ensure the local public key exists.
   - Use the existing local key.
   - Do not create extra workspace-specific keys.
   - If no local public key exists, stop with `needs_input`.
2. Establish host key auth.
   - If key auth already works, reuse it.
   - Otherwise, use the one allowed password bootstrap to append the local public key to `root`’s `authorized_keys` on the host.
3. Probe host prerequisites.
   - Docker must exist.
   - mandatory devices and Ascend mounts must exist
   - host access to `quay.nju.edu.cn/ascend/vllm-ascend` must work, or the image must already be present
4. Decide the container name and SSH port.
5. Pull or reuse the requested image from `quay.nju.edu.cn/ascend/vllm-ascend`.
6. Start the managed container with the canonical host-network NPU recipe.
7. Configure container SSH.
   - install `openssh-server` and `openssh-client`
   - configure a non-default high port
   - allow root key login
   - disable password auth
   - start `sshd`
8. Open the chosen port on the host firewall if a manageable firewall is present.
9. Add the local public key to the container.
10. Verify direct local -> container SSH.
11. Run the container-side NPU smoke test.
12. Persist the record into `.machine-inventory.json`.
13. Best-effort mesh the new container with all other managed containers.

After step 9 succeeds, stop using the bare-metal host except when container SSH later breaks.

### 5. Verify workflow

For `verify` requests:

- stay read-only
- do not silently install packages
- do not silently recreate containers
- do not silently rewrite SSH config

Check, in order:

1. inventory record exists for the target
2. host key SSH works
3. direct container SSH works on the recorded port
4. pinned Python path exists in the container
5. required Ascend env scripts exist in the container
6. NPU smoke test succeeds in the container

If all checks pass, report `ready`.

If direct container SSH fails but host key SSH works, report `needs_repair` and point to the broken layer.

If host key SSH itself fails, report `needs_repair` or `needs_input` depending on whether the request allowed mutation.

### 6. Repair workflow

For `repair` requests, prefer non-destructive fixes first:

- restore host `authorized_keys` entry if key auth drifted and you can still reach the host safely
- re-open the recorded container SSH port on the host firewall
- restart or repair container `sshd`
- re-add the local public key to the container
- re-run mesh key distribution among managed containers
- re-run the container smoke test

Use the bare-metal host only when container SSH is not currently usable.

Do **not** recreate or delete a managed container unless the user explicitly asked for a rebuild or destructive repair. Container recreation is a separate decision because it can discard container-local drift.

### 7. Remove workflow

For `remove` requests:

1. Resolve the inventory record by alias or host IP.
2. Delete only the container recorded there, and only if it is marked as skill-managed.
3. If the recorded container is already absent, treat that as successful drift cleanup.
4. Remove the container endpoint from local `known_hosts`.
5. Best-effort remove the departing container’s mesh key and container endpoint from peer managed containers’ `authorized_keys` and `known_hosts`.
6. Remove the record from `.machine-inventory.json`.

Do not:

- delete other containers
- remove host-level `authorized_keys` entries
- remove host firewall rules
- modify host-to-host trust state

### 8. Mesh behavior

When more than one managed machine exists, silently maintain container-to-container trust.

Rules:

- mesh only the managed containers, not the hosts
- skip unreachable peers without treating that as a hard error
- use stable key comments such as `vaws-mesh:<alias>` so later cleanup is deterministic
- keep the user-facing report focused on the requested machine, not the entire mesh

### 9. Finish with an explicit status report

End with a compact report that covers:

- request type: add / verify / repair / remove
- target host and resolved alias
- container name, container SSH port, and image
- outcome: `ready`, `needs_input`, `needs_repair`, `blocked`, or `removed`
- evidence:
  - host key SSH
  - container SSH
  - NPU smoke test
- mutations performed
- skipped or best-effort steps
- any remaining manual action

## Important edge cases

- If Docker is missing, stop with `blocked`.
- If the local public key is missing, stop with `needs_input`.
- If the host is unreachable, stop with `blocked`.
- If the password is wrong during the one allowed bootstrap, stop instead of retrying blindly.
- If a default container name collides with a non-managed container, choose a different name and record it.
- If the host already has the correct container and direct SSH works, do not recreate it.
- If the image mirror is unreachable and the image is not already local, stop with `blocked`.
- If a peer machine is not reachable for mesh updates, skip it silently and finish the primary request.
- If the user asks to remove a machine that is not in inventory, report that it was not managed by this workspace and stop without guessing.
