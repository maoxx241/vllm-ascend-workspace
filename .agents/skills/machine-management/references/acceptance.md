# Machine-management acceptance criteria

## Trigger examples

These should trigger `machine-management`:

- “帮我配置一下 125.173.1.2 服务器，密码是 1234。”
- “帮我把 125.173.1.3 服务器也配置一下。”
- “检查一下 125.173.1.2 这台机器是不是已经 ready 了。”
- “修一下 125.173.1.2 机器的容器 SSH。”
- “把这台 NPU 机器接进当前 workspace。”
- “帮我移除 125.173.1.6 服务器。”

## Non-trigger examples

These should not trigger `machine-management` unless machine readiness is the obvious blocker:

- “把本地改动同步到远端容器。”
- “在远端容器里重新编译 `vllm` 和 `vllm-ascend`。”
- “启动一个模型服务。”
- “跑 benchmark。”
- “帮我做一个普通的 SSH 连通性测试，不要动 workspace 机器状态。”

## Success criteria

### Universal

- The skill reads local inventory before mutating remote state.
- The skill uses `.machine-inventory.json` as the local state file.
- The skill prefers helper scripts over long inline SSH heredocs.
- The skill does not use `scp`, `sftp`, `sshpass`, or `expect`.
- The final report is compact and evidence-based.

### Add / attach

- The skill prefers host key SSH first and uses a password only for the first bootstrap of a new machine.
- The skill checks Docker and required Ascend/NPU prerequisites before container creation.
- The managed container uses host networking, required devices, required Ascend mounts, and `/vllm-workspace` as the workdir.
- The skill configures a dedicated container `sshd` on a high port without brittle inline edits to `/etc/ssh/sshd_config`.
- The skill verifies direct local -> container SSH.
- The skill runs the smoke test successfully before claiming readiness.
- The skill persists final alias, host identity, container name, image, and SSH port into inventory.

### Verify

- Verify-only runs are read-only.
- A `ready` report requires host SSH, container SSH, and a passing smoke test.
- The skill does not silently repair drift during verify-only requests.

### Repair

- The skill prefers non-destructive repairs first.
- The skill uses the bare-metal host only when container SSH is broken.
- The skill does not recreate or delete a container unless the user explicitly asked for destructive repair.
- The smoke path stays dynamic: no pinned Python patch version, no unconditional vendor `set_env.bash`, no default `devlib` injection.

### Remove

- The skill removes only the container recorded in inventory as skill-managed.
- If the recorded container is already absent, removal still succeeds as drift cleanup.
- The skill removes the local endpoint from `known_hosts`.
- The skill best-effort removes the departing mesh key and endpoint from peers.
- The skill removes the machine record from inventory.
- The skill does not remove host firewall rules or host-level `authorized_keys` entries.

## Manual regression checklist

Review these files together after every substantial skill edit:

- `.agents/skills/machine-management/SKILL.md`
- `.agents/skills/machine-management/references/behavior.md`
- `.agents/skills/machine-management/references/command-recipes.md`
- `.agents/skills/machine-management/references/acceptance.md`
- `.agents/skills/machine-management/scripts/manage_machine.py`
- `.agents/skills/machine-management/scripts/inventory.py`
