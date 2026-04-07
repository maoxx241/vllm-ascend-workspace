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
- the skill prefers the public task wrappers over low-level helper CLIs
- the low-level helper CLIs remain available for deterministic fallback work
- the skill does not use `scp`, `sftp`, `sshpass`, or `expect`
- the final report is compact and evidence-based
- structured wrapper outputs are sufficient for the agent to distinguish `ready`, `needs_input`, `needs_repair`, `blocked`, `removed`, and `unmanaged`
- wrappers stream phase progress on `stderr` as `__VAWS_PROGRESS__=<json>` while keeping the final JSON result on `stdout`

### Public wrapper surface

- normal add / verify / repair / remove flows can be completed through:
  - `machine_add.py`
  - `machine_verify.py`
  - `machine_repair.py`
  - `machine_remove.py`
- normal wrappers do not require the agent to manually call `inventory.py put`, `manage_machine.py remove-container`, or `manage_machine.py verify-machine`
- the wrapper surface is narrower than the low-level helper surface
- alias tolerance stays in parser code, not in the main skill narrative

### Local machine profile

- the skill reuses or creates `.vaws-local/machine-profile.json`
- new machine usernames accept letters and digits only
- usernames normalize to lowercase
- default/random generation happens only after explicit user consent
- `workspace_profile.py ensure` on a missing profile fails unless `--username` or `--generate` is provided
- new managed container names derive from that namespace instead of a single global fixed name

### Add / attach

- `machine_add.py` can succeed with `--host --image rc` when the profile and host key SSH are already in place
- when the profile is missing, `machine_add.py` returns `needs_input` instead of silently generating a username
- when the image choice is missing for a new machine, `machine_add.py` returns `needs_input` instead of silently defaulting to `auto`, `latest`, or another moving tag
- when an existing inventory record still points at a legacy or moving image tag, `machine_add.py` returns `await-image-selection` before any `already-ready` shortcut
- the skill prefers host key SSH first and uses a password only for the first bootstrap of a new machine
- if the user already supplied the host password in the request, the skill prefers scripted bootstrap before asking the user to run a manual command
- the primary bootstrap path does not depend on `ssh-copy-id`
- the skill checks Docker and required Ascend/NPU prerequisites before container creation
- the managed container uses host networking, required devices, required Ascend mounts, and `/vllm-workspace` as the workdir
- the skill configures a dedicated container `sshd` on a high port without brittle inline edits to `/etc/ssh/sshd_config`
- the container bootstrap ensures `/run/sshd` exists
- space-containing remote arguments such as SSH public keys and mesh peer keys survive the SSH hop intact
- `machine_add.py` persists final alias, namespace, host identity, container name, image, and SSH port into inventory without the agent having to call `inventory.py put`
- the recorded inventory image is the actual selected image after mirror resolution and pull / cache fallback
- `rc`, `main`, and `stable` remain first-class selectors, while `auto`, `*:latest`, and bare repositories without a tag are rejected as defaults
- long-running bootstrap phases keep emitting attributable progress for image pull and package-install steps instead of going silent behind one global timeout
- inventory `put` / `remove` writes are atomic and serialized so concurrent wrappers do not clobber the recorded machine set

### Verify

- verify-only runs are read-only
- a `ready` report requires host SSH, container SSH, and a passing smoke test
- verify and smoke paths have an overall timeout budget, not only SSH connect timeouts
- the skill does not silently repair drift during verify-only requests

### Repair

- `machine_repair.py` accepts a single machine identifier for the normal case
- `machine_repair.py` can request an explicit replacement image when the recorded image is legacy, ambiguous, or the user wants to rotate tracks
- `machine_repair.py` does not short-circuit to `already-ready` when the recorded image is legacy, ambiguous, or still points at a moving tag
- the skill prefers non-destructive repairs first
- the skill uses the bare-metal host only when container SSH is broken
- the skill does not recreate or delete a container unless the user explicitly asked for destructive repair
- the smoke path stays dynamic: no pinned Python patch version, no unconditional vendor `set_env.bash`, no default `devlib` injection

### Remove

- `machine_remove.py` removes only the container recorded in inventory as skill-managed
- if the recorded container is already absent, removal still succeeds as drift cleanup
- the skill removes the local endpoint from `known_hosts`
- the skill best-effort removes the departing mesh key and endpoint from peers
- the skill removes the machine record from inventory
- the skill does not remove host firewall rules or host-level `authorized_keys` entries

## Regression checklist from prior sessions

These specific mistakes should no longer be part of the normal path:

- agent should not need `manage_machine.py probe-host --host-user root`
- agent should not need `inventory.py put ...` just to finish a normal add flow
- agent should not need `manage_machine.py remove-container ... --container-name ...` for a normal remove flow
- agent should not need to inspect helper `--help` output just to recover from ordinary add / verify / repair / remove work

## Manual regression checklist

Review these files together after every substantial skill edit:

- `.agents/skills/machine-management/SKILL.md`
- `.agents/skills/machine-management/references/behavior.md`
- `.agents/skills/machine-management/references/command-recipes.md`
- `.agents/skills/machine-management/references/acceptance.md`
- `.agents/skills/machine-management/scripts/machine_add.py`
- `.agents/skills/machine-management/scripts/machine_verify.py`
- `.agents/skills/machine-management/scripts/machine_repair.py`
- `.agents/skills/machine-management/scripts/machine_remove.py`
- `.agents/skills/machine-management/scripts/manage_machine.py`
- `.agents/skills/machine-management/scripts/inventory.py`
- `.agents/scripts/workspace_profile.py`
- `.agents/lib/vaws_local_state.py`
