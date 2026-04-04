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

- the skill reads local profile and inventory state before mutating remote state
- the skill uses `.vaws-local/` as the canonical local runtime-state directory
- the skill prefers helper scripts over long inline SSH heredocs
- the skill does not use `scp`, `sftp`, `sshpass`, or `expect`
- the final report is compact and evidence-based

### Local machine profile

- the skill reuses or creates `.vaws-local/machine-profile.json`
- new machine usernames accept letters and digits only
- usernames normalize to lowercase
- blank input generates a default namespace
- new managed container names derive from that namespace instead of a single global fixed name

### Add / attach

- the skill prefers host key SSH first and uses a password only for the first bootstrap of a new machine
- password bootstrap uses `bootstrap-host-key` or an equivalent single foreground interactive step
- the skill never passes the password via shell args, files, env vars, or password automation
- the skill checks Docker and required Ascend/NPU prerequisites before container creation
- the managed container uses host networking, required devices, required Ascend mounts, and `/vllm-workspace` as the workdir
- the skill configures a dedicated container `sshd` on a high port without brittle inline edits to `/etc/ssh/sshd_config`
- the skill verifies direct local -> container SSH
- the skill runs the smoke test successfully before claiming readiness
- the skill persists final alias, namespace, host identity, container name, image, and SSH port into inventory

### Verify

- verify-only runs are read-only
- a `ready` report requires host SSH, container SSH, and a passing smoke test
- the skill does not silently repair drift during verify-only requests

### Repair

- the skill prefers non-destructive repairs first
- the skill uses the bare-metal host only when container SSH is broken
- the skill does not recreate or delete a container unless the user explicitly asked for destructive repair
- the smoke path stays dynamic: no pinned Python patch version, no unconditional vendor `set_env.bash`, no default `devlib` injection

### Remove

- the skill removes only the container recorded in inventory as skill-managed
- if the recorded container is already absent, removal still succeeds as drift cleanup
- the skill removes the local endpoint from `known_hosts`
- the skill best-effort removes the departing mesh key and endpoint from peers
- the skill removes the machine record from inventory
- the skill does not remove host firewall rules or host-level `authorized_keys` entries

## Manual regression checklist

Review these files together after every substantial skill edit:

- `.agents/skills/machine-management/SKILL.md`
- `.agents/skills/machine-management/references/behavior.md`
- `.agents/skills/machine-management/references/command-recipes.md`
- `.agents/skills/machine-management/references/acceptance.md`
- `.agents/skills/machine-management/scripts/manage_machine.py`
- `.agents/skills/machine-management/scripts/inventory.py`
- `.agents/scripts/workspace_profile.py`
- `.agents/lib/vaws_local_state.py`
