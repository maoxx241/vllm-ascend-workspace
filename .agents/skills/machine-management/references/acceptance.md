# Machine-management acceptance criteria

This file is for validating the `machine-management` skill.

## Trigger examples

These should trigger `machine-management`:

- “帮我配置一下 125.173.1.2 服务器，密码是 1234。”
- “帮我把 125.173.1.3 服务器也配置一下，密码和 125.173.1.2 一样。”
- “帮我配置一下 125.173.1.4 服务器，我已经把公钥加进去了你可以直接登录。”
- “帮我加一下 125.173.1.5 服务器。”
- “帮我移除 125.173.1.6 服务器。”
- “检查一下 125.173.1.2 这台机器是不是已经 ready 了。”
- “修一下 125.173.1.2 机器的容器 SSH。”
- “把这台 NPU 机器接进当前 workspace。”

## Non-trigger examples

These should not trigger `machine-management` unless machine readiness is the obvious blocker:

- “把本地改动同步到远端容器。”
- “在远端容器里重新编译 `vllm` 和 `vllm-ascend`。”
- “启动一个模型服务。”
- “跑 benchmark。”
- “修一下这个 `vllm-ascend` bug。”
- “更新 README 文案。”
- “帮我做一个普通的 SSH 连通性测试，不要动 workspace 机器状态。”

## Success criteria

A successful run should satisfy all applicable items below.

### Universal

- The skill reads the local inventory before mutating remote state.
- The skill uses `.machine-inventory.json` as the local state file.
- The skill keeps machine metadata out of tracked files other than the public skill package.
- The skill distinguishes add / verify / repair / remove correctly.
- The skill does not use `scp`, `sftp`, `sshpass`, or `expect`.
- The skill reports a compact final outcome with evidence and remaining manual action, if any.

### Add / attach

- The skill prefers host key SSH first and uses a password only for the first bootstrap of a new machine.
- The skill does not continue using the password after host key auth is established.
- The skill checks Docker and required NPU/Ascend prerequisites before container creation.
- The skill pulls the image from `quay.nju.edu.cn/ascend/vllm-ascend` unless the image is already present locally.
- The managed container uses host networking, the required devices, the required Ascend mounts, and `/vllm-workspace` as the workdir.
- The skill installs and configures container SSH on a high non-default port.
- The skill adds the local public key to the container and verifies direct local -> container SSH.
- The skill persists the final alias, host identity, container name, image, and SSH port into inventory.

### Verify

- Verify-only runs are read-only.
- A `ready` report requires host key SSH, container SSH, and a passing NPU smoke test.
- The skill checks the pinned container Python path and Ascend env scripts before claiming readiness.
- The skill does not silently repair drift during verify-only requests.

### Repair

- The skill prefers non-destructive repairs first.
- The skill uses the bare-metal host only when container SSH is broken.
- The skill does not recreate or delete a container unless the user explicitly asked for destructive repair.
- The skill can restore mesh trust without changing host-level trust.

### Remove

- The skill removes only the container recorded in inventory as skill-managed.
- If the recorded container is already absent, the skill still succeeds as drift cleanup.
- The skill removes the container endpoint from local `known_hosts`.
- The skill best-effort removes the departing mesh key and endpoint from peer managed containers.
- The skill removes the machine record from inventory.
- The skill does not remove host firewall rules.
- The skill does not remove host-level `authorized_keys` entries.

## Scenario matrix

| Scenario | Expected result |
| --- | --- |
| new host, password provided, no existing key auth | one interactive host bootstrap, managed container created, direct container SSH verified, inventory written |
| new host, key auth already works | skip password, create or verify managed container, inventory written |
| add request, but local public key is missing | `needs_input`; no container creation |
| add request, but Docker is missing on the host | `blocked`; no host package-manager install in v1 |
| verify request, host and container both healthy | `ready` with host SSH, container SSH, and NPU smoke evidence |
| verify request, host key SSH works but container SSH is broken | `needs_repair`; no mutation during verify-only flow |
| repair request, container SSH is broken but host key SSH works | repair through host maintenance plane, restore direct container SSH if possible |
| repair request would require container recreation | stop for user input unless the user explicitly asked for destructive repair |
| remove request, recorded container already absent | `removed`; clean local/peer trust best-effort and remove inventory record |
| remove request for a host not in inventory | do not guess; report that the machine is not managed by this workspace |
| multiple managed machines exist but some peers are not reachable | primary request succeeds; mesh updates skip unreachable peers silently |
| host has proxy vars but they do not improve outbound reachability | do not pass proxy env into the container |

## Manual regression checklist

Review these files together after every substantial skill edit:

- `.agents/skills/machine-management/SKILL.md`
- `.agents/skills/machine-management/references/behavior.md`
- `.agents/skills/machine-management/references/command-recipes.md`
- `.agents/skills/machine-management/references/acceptance.md`
- `.agents/skills/machine-management/scripts/inventory.py`
