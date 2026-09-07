# Access and infrastructure family audit

Scope: `.agents/skills/repo-init/`, `.agents/skills/machine-management/`,
`.agents/skills/session-management/`, `.agents/skills/remote-toolbox/`,
`.agents/skills/npu-fleet-monitor/`, plus the shared code they own or are
owned by: `.agents/scripts/remote_*.py`, `.agents/lib/vaws_remote_toolbox.py`,
`.agents/lib/vaws_session_*.py`, `.agents/lib/vaws_npu_coordination.py`.

Read for cross-checking, not audited: `.remote-dev/` (target resolution and
result contract), `.agents/coordinator/` (host authority and pool bindings),
`.agents/knowledge/known-failure-signatures.yaml`.

This document changes no behaviour. Every claim carries `file:line` evidence.
No host addresses, container names, credentials or absolute user paths are
reproduced here.

## 0. Headline numbers

| Measure | Value |
| --- | --- |
| Entry-point files in the five skill directories | 20 |
| Additional entry-point files these skills own under `.agents/scripts/` | 19 |
| Verb-level commands exposed by those 39 files | 73 |
| Python lines in the five skill directories | 13,083 |
| Python lines in the shared libs they depend on | 9,276 (`.agents/lib` + `.agents/scripts`) |
| Distinct argument→endpoint resolution paths | 9 argument shapes, 9 independent implementations |
| Resolution paths that should survive the split | 4 argument shapes, 2 producers |
| Verbs classified redundant | 29 of 73 |
| Proposed surviving verbs for this family | 28 across 5 entry points (+4 in the extracted monitor repo) |

The count of 73 verbs from 39 files is the real agent-facing surface. The
brief's "20 entry points" is the file count inside the skill directories; the
`.agents/scripts/remote_*.py` wrappers are the same family's surface wearing a
different directory (`.agents/README.md:239-248` names them as part of the
`remote-toolbox` maintenance unit).

## 1. Capability inventory

### 1.1 `repo-init` — 5 files, 10 verbs

| Entry point | Verbs | Real parameters | What it does | Callers |
| --- | --- | --- | --- | --- |
| `scripts/repo_init_probe.py` | (single) | `--compact` | Read-only probe of platform, `gh`, auth, submodules, remotes, forks, machine profile, workspace identity; silently ensures the local UUID4; emits a `decision_checkpoint` payload with fixed question templates (`repo_init_probe.py:507-578`) | Agent only (`SKILL.md:51-54`) |
| `scripts/repo_init_profile.py` | `plan`, `apply`, `apply-alias` | `--choice git-username\|random\|custom`, `--custom-username`, `--custom-alias` | Materializes one approved machine-username / alias choice; wraps `workspace_profile.py` and `workspace_identity.py` so broad init cannot call `ensure` bare (`repo_init_profile.py:287-323`) | Agent; `_profile_choice_common.py` shared with the probe |
| `scripts/repo_topology.py` | `compare-main`, `configure`, `ensure-main` | `--repo`, `--origin-url`, `--upstream-url`, `--gh-default`, `--remote`, `--branch` | Compares and rewrites `origin`/`upstream` and branch tracking for the workspace and each submodule (`repo_topology.py:286-338`) | Agent |
| `scripts/resolve_vllm_ci_pin.py` | (single) | `--vllm-ascend-dir` | Resolves the vLLM commit CI verifies, preferring `.github/vllm-main-verified.commit` and reporting which source was used (`resolve_vllm_ci_pin.py:164-172`) | Agent, step 3a of the skill workflow |
| `scripts/install_gh_user.py` + `scripts/install-gh-user.ps1` | (single, ×2 platforms) | none — no argparse | Downloads and installs `gh` into the user prefix; prints human text, not JSON (`install_gh_user.py:103-151`) | Agent, POSIX/Windows twin per `SKILL.md:44-48` |

`repo-init` also has one test file outside a `tests/` directory
(`test_workspace_identity.py`), which is why it is not discovered by CI (see
§2.5).

### 1.2 `machine-management` — 6 files, 19 verbs

| Entry point | Verbs | Real parameters | What it does | Callers |
| --- | --- | --- | --- | --- |
| `scripts/machine_add.py` | (single) | `--host`, `--alias`, `--host-user`, `--host-port`, `--machine-username`, `--generate-machine-username`, `--image`, `--machine-type`, `--workdir`, `--public-key-file`, `--password{,-env,-stdin}` | Full attach path: profile gate → image gate → key → host auth → probe → machine-type/SoC detection → container bootstrap → inventory write → mesh → final verify (`machine_add.py:83-397`) | Agent |
| `scripts/machine_verify.py` | (single) | `--machine`, `--python` | Read-only host SSH + container SSH + `torch`/`torch_npu` smoke; stamps `last_verified_at` only on success (`machine_verify.py:33-71`, `_workflow_common.py:811-826`) | Agent; `machine_add`/`machine_repair` call the same helper in-process |
| `scripts/machine_repair.py` | (single) | `--machine`, `--image`, `--public-key-file`, `--python`, `--machine-type`, `--password{,-env,-stdin}` | Conservative repair: verify-before → image gate → host auth → probe → bootstrap → inventory → mesh → verify-after (`machine_repair.py:64-326`) | Agent |
| `scripts/machine_remove.py` | (single) | `--machine` | Mesh trust cleanup → container removal → parity-state cleanup → inventory removal (`machine_remove.py:35-78`) | Agent; `_workflow_common.remove_container` also called by `session_remove.py:20` |
| `scripts/manage_machine.py` | `probe-host`, `bootstrap-host-key`, `bootstrap-container`, `smoke`, `verify-machine`, `mesh-export-key`, `mesh-add-peer`, `mesh-remove-peer`, `clean-local-known-hosts`, `remove-container` | per-subcommand `--host/--user/--host-port/--container-ssh-port/--image/--workdir/--namespace/...` (`manage_machine.py:3231-3523`) | 3,535-line implementation layer: remote script rendering, image resolution, SoC detection, ATB/apt/pip pinning, progress protocol | **Imported as a library** by `_workflow_common.py:28`, `machine_add.py:14`, `machine_repair.py:14`, `session_create.py:26`; also a full CLI |
| `scripts/inventory.py` | `summary`, `get`, `put`, `upsert`, `remove` | `--inventory`, `--alias`, `--host-ip/--host`, `--host-port`, `--host-user`, `--host-machine-type`, `--host-soc`, `--container-name`, `--workdir`, … (`inventory.py:439-568`) | Inventory store: schema validation, locking, shared-worktree path resolution | **Imported** by `_workflow_common.py:27`, `session_create.py:25`, `vllm-ascend-serving/scripts/_common.py:26`; `vaws_remote_toolbox.py:267-291` and `coordinator/backend.py:16` read the same file through `_load_inventory` |

### 1.3 `session-management` — 8 files, 21 verbs

| Entry point | Verbs | Real parameters | What it does | Callers |
| --- | --- | --- | --- | --- |
| `scripts/session_create.py` | (single) | `--machine` (required), `--session-id`, `--base-ref`, `--branch`, `--worktree-root`, `--no-worktree`, `--image`, `--devices`, `--npu-count`, `--container-ssh-port{,-range}`, `--runtime-profile`, `--runtime-root`, `--workdir`, `--reuse-existing`, `--disable-prepared-image-cache`, `--verification-mode`, `--replace-container-on-image-change`, `--public-key-file`, hidden `--skip-container-bootstrap` (`session_create.py:418-449`) | Worktree + submodule branches, lease allocation, session container bootstrap, SSH/visibility verification, worktree binding, `next_steps` | Agent; imports `inventory`, `manage_machine`, `_workflow_common`, and `session_gc.probe_container_alive` (`session_create.py:25-43`) |
| `scripts/session_list.py` | (single) | `--active-only` | Index + lease listing | Agent; `remote-toolbox` acceptance docs |
| `scripts/session_status.py` | (single) | `--session-id`, `--session-file` | Live container SSH, device-visibility probe, service PID check, live leases; downgrades `ready`→`needs_repair` on drift (`session_status.py:158-164`) | Agent |
| `scripts/session_diff.py` | (single) | `--session-id`, `--session-file`, `--stat` | Scaffold + submodule diff against recorded bases | Agent |
| `scripts/session_remove.py` | (single) | `--session-id`, `--session-file`, `--remove-container`, `--remove-worktree`, `--release-leases`, `--force` | Stop service → remove container → remove worktree → release leases, each gated on proof (`session_remove.py:124-224`) | Agent; `vaws_remote_toolbox.cleanup` shells out to it (`vaws_remote_toolbox.py:1889-1896`) |
| `scripts/session_gc.py` | (single) | `--dry-run` (default), `--apply`, `--reap-dead` | Stale-metadata report; releases leases only after host Docker state plus repeated free-device samples (`session_gc.py:173-219`) | Agent; `probe_container_alive` imported by `session_create.py:43` |
| `scripts/session_group.py` | `create`, `status`, `list`, `teardown` | `--group-id`, `--member name=id`, `--startup-order`, `--remove-containers`, `--remove-worktrees`, `--release-leases`, `--force` | Binds ready sessions with code/submodule parity and ordered startup/shutdown | Agent |
| `scripts/npu_coordination.py` | `submit`, `acquire`, `preflight`, `activate`, `heartbeat`, `release`, `cancel`, `status`, `gc`, `hold-add`, `hold-remove` | target group `--machine\|--session-id\|--session-file\|--host` + `--host-port`, `--host-user`, `--state-dir`, `--timeout`, plus per-verb ids/tokens (`npu_coordination.py:154-252`) | Ships `vaws_npu_coordination.py` to the bare-metal host and executes the SQLite queue protocol there | Agent; **`.agents/coordinator/backend.py:19-21` loads this file by path** and uses `ssh_execute`/`build_remote_command` as its host authority adapter |

### 1.4 `remote-toolbox` — 0 files in the skill directory, 19 owned entry points

The skill package is documentation only (`SKILL.md`, four references). All
behaviour lives in `.agents/lib/vaws_remote_toolbox.py` (2,341 lines) and is
exposed through 18 fifteen-line wrappers plus one stress harness:

| Entry point | CLI function | Real parameters | What it does |
| --- | --- | --- | --- |
| `remote_target_resolve.py` | `cli_target_resolve` (`vaws_remote_toolbox.py:1981`) | `--machine`, `--session-id`, `--session-file` | Prints the resolved target dict |
| `remote_probe.py` | `cli_probe:1995` | target + `--timeout` | Host `docker inspect`, container runtime facts (CANN, Python, torch, torch_npu, vllm, vllm_ascend, `npu-smi`), serving state, `known_hosts` facts |
| `remote_exec.py` | `cli_exec:2009` | target + `--cwd`, `--env`, `--timeout`, `--no-runtime-env`, `--command`/`-- …` | One bounded SSH command with full local logs |
| `remote_job_start.py` | `cli_job_start:2052` | target + `--cwd`, `--env`, `--kind`, `--job-id`, `--timeout`, `--no-runtime-env`, `--command` | `nohup` job with remote status/pid files and a local job record |
| `remote_job_status.py` | `cli_job_status:2088` | target + `--job-id` | Remote status + pid liveness |
| `remote_job_tail.py` | `cli_job_tail:2104` | `--job-id`, `--lines`, `--stream` | Log tail |
| `remote_job_stop.py` | `cli_job_stop:2121` | `--job-id`, `--force` | Signal + status finalize |
| `remote_job_collect.py` | `cli_job_collect:2138` | `--job-id`, `--local-dir` | `artifact_pull` of the job dir |
| `remote_sync_plan.py` | `cli_sync_plan:2155` | target + `--mode`, `--force-reinstall` | Delegates to `remote-code-parity` plan and classifies install reasons |
| `remote_sync_apply.py` | `cli_sync_apply:2170` | target + `--mode`, `--force-reinstall`, `--dry-run` | Shells out to `parity_sync.py --apply-mode` |
| `remote_service_start.py` | `cli_service_start:2187` | target + passthrough | Shells out to `serve_start.py` |
| `remote_service_status.py` | `cli_service_status:2208` | target | Shells out to `serve_status.py` |
| `remote_service_logs.py` | `cli_service_logs:2222` | target + `--lines` | Tails the recorded serving logs directly over SSH |
| `remote_service_stop.py` | `cli_service_stop:2237` | target + `--force` | Shells out to `serve_stop.py` |
| `remote_artifact_manifest.py` | `cli_artifact_manifest:2252` | target + `--remote-path` | SHA-256 manifest |
| `remote_artifact_pull.py` | `cli_artifact_pull:2267` | target + `--remote-path`, `--local-dir` | `tar`-batched or per-file `cat` pull with hash verify |
| `remote_artifact_push.py` | `cli_artifact_push:2283` | target + `--local-path`, `--remote-path` | Streamed push with remote `sha256sum` verify |
| `remote_cleanup.py` | `cli_cleanup:2299` | target + `--dry-run`, `--jobs`, `--job-id`, `--service`, `--session-container`, `--leases`, `--known-hosts`, `--remote-temp`, `--all`, `--force` | Proof-gated cleanup of service, jobs, temp, container, leases, `known_hosts` |
| `remote_toolbox_stress.py` | own parser (`remote_toolbox_stress.py:41-56`) | target + `--parallelism`, `--exec-count`, `--job-count`, `--artifact-files`, `--artifact-bytes`, `--timeout`, `--skip-*`, `--cleanup-remote` | Repeatable non-NPU pressure harness |

Consumers of the same library, invisible to a grep for the wrapper names:
`vllm-ascend-serving/scripts/_common.py:29`, `vllm-ascend-benchmark/scripts/_common.py:21`,
`ascend-profiling-collection/scripts/_common.py:62`,
`ascend-profiling-analysis/scripts/_common.py:36`,
`ascend-memory-profiling/scripts/_common.py:24`,
`.remote-dev/core/endpoint.py:118`, `.agents/coordinator/backend.py:16`.

### 1.5 `npu-fleet-monitor` — 1 file, 4 verbs

| Entry point | Verbs | Real parameters | What it does | Callers |
| --- | --- | --- | --- | --- |
| `scripts/manage_monitor.py` | `ensure`, `status`, `restart`, `stop` | `--branch`, `--worktree` | Resolves/creates the `vaws-top` worktree, validates required files and cleanliness, builds if the locked commit changed, installs and restarts the user service, reports `systemctl` properties plus loopback `/api/health` (`manage_monitor.py:230-272`) | Agent only. Nothing in `.agents/`, `.remote-dev/` or `.agents/coordinator/` imports it or queries the monitor API |

## 2. Classification

Tally over the 73 verbs: **39 closed-world mechanics**, **4 mixed**,
**1 open-world judgment**, **29 redundant**.

### 2.1 Closed-world mechanics (39)

Each should end up behind one consolidated command. "Maturity evidence" is what
real-hardware validation would make it boring.

| Verb(s) | Evidence | Consolidated owner | Real-hardware validation that would mature it |
| --- | --- | --- | --- |
| `machine_remove` | `machine_remove.py:35-78` | `vaws machine remove` | Remove one of ≥3 meshed hosts, prove the departing peer key is gone from the survivors and that inventory, `known_hosts` and parity state have exactly one record removed |
| `inventory summary` | `inventory.py:454-456` | `vaws machine list` (today there is **no** public list verb — `machine_verify` requires `--machine`) | Listing from a linked worktree resolves the primary-worktree inventory and never falls back (`_workflow_common.py:299-303`) |
| `session_list`, `session_status`, `session_diff`, `session_remove`, `session_gc` | `session_list.py:34-68`, `session_status.py:94-183`, `session_diff.py:102-110`, `session_remove.py:148-243`, `session_gc.py:111-170` | `vaws session <list\|status\|diff\|remove\|gc>` | Two concurrent sessions on one host: distinct container names, SSH ports, leases and state dirs; kill one container out of band and prove `session_status` reports `needs_repair` and `session_gc --reap-dead --apply` releases only that session's cards |
| `session_group create\|status\|list\|teardown` | `session_group.py:457-482` | `vaws session group <verb>` | A ≥2-member group with dirty worktrees: content-level parity check, ordered startup, reverse shutdown, no duplicate leases |
| `npu_coordination submit\|acquire\|preflight\|activate\|heartbeat\|release\|cancel\|status\|gc\|hold-add\|hold-remove` (11) | `npu_coordination.py:176-252`, protocol in `vaws_npu_coordination.py:314-420` | `vaws npu <verb>` — and this is the piece that should move into the coordinator repository, because `coordinator/backend.py:19-21` already treats it as the host authority | Three independent agents plus one human `hold` on one host: FIFO grants, fence tokens survive a `/tmp` epoch reset, `release` requires repeated free samples, and a CPU-initializing job with an empty NPU process table keeps its cards (`vaws_npu_coordination.py:55-87`) |
| `remote_service_logs` | `vaws_remote_toolbox.py:1799-1833` | `serve logs` — the serving family has no logs verb, so this one is not redundant, it is *misfiled* | Tail a live multi-rank service log and prove the stream does not die on the shared SSH mux (see §6.3) |
| `remote_cleanup` | `vaws_remote_toolbox.py:1836-1946` | `vaws cleanup` | Cleanup after a failed service start: `--leases` without `--service` returns `blocked` (`:1897-1905`), and a failed service stop refuses lease release (`:1908-1915`) |
| `remote_toolbox_stress` | `remote_toolbox_stress.py:41-56` | move under `.agents/tests/` — it is validation tooling, not an agent capability | Already has a documented matrix (`references/stress-validation.md`) |
| `repo_init_probe` | `repo_init_probe.py:507-578` | `repo-init probe` | Clean clone, drifted clone, and uninitialized-submodule clone all produce a complete `decision_checkpoint` without mutating anything but the UUID file |
| `repo_init_profile plan\|apply\|apply-alias` (3) | `repo_init_profile.py:287-323` | `repo-init profile <verb>` | `custom` never silently degrades to the detected Git username on a machine with a `gh` login, an `origin` owner and a `user.name` all disagreeing (`_profile_choice_common.py:142-152`) |
| `repo_topology compare-main\|configure\|ensure-main` (3) | `repo_topology.py:286-338` | `repo-init topology <verb>` | `configure --repo <submodule>` on an **uninitialized** submodule must refuse rather than resolve to the parent repo — the failure mode `SKILL.md:178` warns about |
| `resolve_vllm_ci_pin` | `resolve_vllm_ci_pin.py:164-172` | `repo-init ci-pin` | Old checkout with only a workflow matrix, and a new one with the verified-commit file, both report the source used |
| `install_gh_user` (+ `.ps1`) | `install_gh_user.py:103-151` | `repo-init install-gh` | macOS, Linux, WSL and Windows installs land on `PATH` and `gh --version` works; twin scripts stay behaviour-identical |
| `manage_monitor ensure\|status\|restart\|stop` (4) | `manage_monitor.py:230-272` | stays as `monitor <action>` in the extracted monitor repository | Deploy on a fresh host: build only when the locked commit changed, listener bound to loopback, ignored `data/` preserved across `restart` |

### 2.2 Open-world judgment (1)

| Verb | Evidence | What guidance replaces the script |
| --- | --- | --- |
| `machine_repair` | `machine_repair.py:64-326` | The mechanics inside it are all reachable elsewhere: `verify_machine` (`_workflow_common.py:645-714`), `bootstrap_host_key` (`:429-482`), `probe_host` (`:485-515`), `bootstrap_container` (`:534-624`), `upsert_machine_record` (`:835-929`), `sync_mesh` (`:754-788`). What `machine_repair` actually encodes is a *policy* — repair non-destructively, in this order, and stop before recreating a container. That belongs in `SKILL.md` as an escalation ladder plus the "what counts as ready" definition (`machine-management/SKILL.md:10-15`, `:202-210`), with the agent choosing which mechanic to run against the observed drift. Keeping it as one opaque command is why it is 336 lines of ordering logic that duplicates `machine_add` almost line for line (compare `machine_add.py:196-330` with `machine_repair.py:158-268`) |

Everything else the family treats as judgment is already handled the right way:
the image decision is a structured `needs_input` payload rather than a default
(`_workflow_common.py:131-208`, `:224-249`), and the username/alias/topology
decisions are fixed-option question payloads
(`_profile_choice_common.py:159-239`, `repo_init_probe.py:548-576`).

### 2.3 Mixed — where the seam is (4)

| Verb | The mechanic | The judgment | Exact seam |
| --- | --- | --- | --- |
| `machine_add` | key install, host auth, probe, container bootstrap, inventory write, mesh, verify | which image; whether a machine that verifies `ready` but records a moving image tag should be reused | The seam is already cut correctly at `machine_add.py:108-115` → `resolve_workflow_image` returns `(None, needs_input)` instead of defaulting, and `:130-138` refuses the `already-ready` shortcut when the recorded image no longer matches. The remaining mixing is that machine-type detection *ends* in a judgment: `machine_add.py:249-259` returns `blocked` asking for `--machine-type`, which is correct, but `:242-248` silently prefers a recorded/inferred type over a fresh probe when the probe is merely absent — the SoC precedence rule in `SKILL.md:51-52` is stronger than the code |
| `machine_verify` | two SSH checks + `torch`/`torch_npu` smoke | "is this machine healthy enough to trust for my task" | Seam is at `_workflow_common.py:700` — `ready` is a single boolean AND of host SSH, container SSH and smoke. The mechanic should return the three facts; the trust decision is the agent's, and `machine-management/SKILL.md:15` already says ready does not imply sync/serve/bench readiness. The `blocked` branch at `:676-692` (missing local `ssh`) is a good example of layer-correct reporting and should be the model |
| `session_create` | worktree, submodule branches, container bootstrap, port lease, binding write | how many cards, which cards, whether to trust the prepared-image cache, how much verification is enough | Seam is at `session_create.py:537-547`: `--devices` (explicit, agent decides) vs `--npu-count` (script decides which cards) — see §4. Second seam at `:437-446`: `--verification-mode` defaults to `ssh`, i.e. the script decides that NPU proof can be deferred |
| `remote_probe` | collect host `docker inspect`, container runtime facts, `known_hosts` facts | is the runtime the right one | Seam is at `vaws_remote_toolbox.py:812-822`: the payload deliberately reports `recorded_tag` next to observed `image_id` with the note that the tag is untrusted — that is the right shape. What is mixed is the top-level `status`, which collapses to `needs_repair` on any container-fact failure regardless of plane (§6.5) |

### 2.4 Redundant (29) — with the surviving owner

**`.agents/scripts/remote_*.py` duplicating `.remote-dev` (15).** `.agents/README.md:190-200`
and `remote-toolbox/SKILL.md:15-18` already declare `.remote-dev` the preferred
surface, so these are the compatibility layer, not the owner.

| Redundant verb | Surviving owner | Evidence |
| --- | --- | --- |
| `remote_exec` | `remote.bash` | `vaws_remote_toolbox.py:556-624` vs `.remote-dev/core/shell_ops.py:50-175`; both source `/etc/profile.d/vaws-ascend-env.sh`, `cd` into a cwd, export env, run one bash command, and write local stdout/stderr/meta |
| `remote_job_start` | `remote.bash --run-in-background` | `vaws_remote_toolbox.py:908-1022` vs `.remote-dev/core/shell_ops.py:86-94` → `job_ops.start_remote_job` |
| `remote_job_status` | `remote.job_status` | `vaws_remote_toolbox.py:1045-1083` vs `.remote-dev/tools/remote_job_status.py` |
| `remote_job_tail` | `remote.job_tail` | `:1086-1108` |
| `remote_job_stop` | `remote.job_stop` | `:1111-1144` |
| `remote_job_collect` | `remote.artifact_pull` | `:2138-2152` — it is literally `artifact_pull(remote_path=record["remote_dir"])` |
| `remote_artifact_manifest` | `remote.artifact_manifest` | `:1147-1196` vs `.remote-dev/core/artifact_ops.py` |
| `remote_artifact_pull` | `remote.artifact_pull` | `:1213-1291` |
| `remote_artifact_push` | `remote.artifact_push` | `:1460-1531` |
| `remote_target_resolve` | one resolver + `remote.probe` | `:1981-1992` prints what `.remote-dev/core/endpoint.py:47-62` already puts in every result's `target` block |
| `remote_probe` (as a second probe implementation) | `remote.probe` | `:685-831` vs `.remote-dev/core/context_snapshot.py:89-...`; both inspect `torch`, `torch_npu`, `vllm`, `vllm_ascend` and repo heads |
| `remote_sync_plan` | `remote-code-parity` `remote_code_parity.py plan` | `:1647-1713` calls `parity_derived_args` + `_parity_plan_manifest`, both of which shell out to parity scripts (`:1570-1610`) |
| `remote_sync_apply` | `parity_sync.py --apply-mode` | `:1716-1751` — a pure subprocess wrapper |
| `remote_service_start` | `serve_start.py` | `:1754-1796` `call_service("start", …)` |
| `remote_service_status` / `remote_service_stop` | `serve_status.py` / `serve_stop.py` | same dispatcher, `:1758-1761` |

**`manage_machine.py` subcommands duplicating the four wrappers (10).** The
wrappers already sequence every one of them, and the module is imported as a
library by four callers (`_workflow_common.py:28`, `machine_add.py:14`,
`machine_repair.py:14`, `session_create.py:26`). Surviving owner: the library
plus `vaws machine <add|verify|repair|remove>`.

| Redundant verb | Where the wrapper already runs it |
| --- | --- |
| `probe-host` | `_workflow_common.probe_host:485` ← `machine_add.py:198`, `machine_repair.py:159` |
| `bootstrap-host-key` | `_workflow_common.bootstrap_host_key:429` ← `machine_add.py:187` |
| `bootstrap-container` | `_workflow_common.bootstrap_container:534` ← `machine_add.py:282`, `machine_repair.py:220`, `session_create.py:662` |
| `smoke` | `_workflow_common.smoke_machine:626` ← `verify_machine:696` |
| `verify-machine` | `_workflow_common.verify_machine:645` ← all three wrappers |
| `mesh-export-key`, `mesh-add-peer`, `mesh-remove-peer` | `sync_mesh:754` / `cleanup_mesh:790` ← `machine_add.py:323`, `machine_repair.py:261`, `machine_remove.py:53` |
| `clean-local-known-hosts` | `remove_container:972` calls `remove_known_host_entry` (`_workflow_common.py:995-999`) |
| `remove-container` | `_workflow_common.remove_container:972` ← `machine_remove.py:55`, `session_remove.py:185` |

**`inventory.py` write and lookup verbs (4).**

| Redundant verb | Surviving owner | Evidence |
| --- | --- | --- |
| `upsert` | `put` | `inventory.py:512` — the help string says "alias of put"; the two parsers are 50 duplicated lines (`:461-511` vs `:512-562`) |
| `put` | `vaws machine add/repair` | `_workflow_common.upsert_machine_record:835-929` is the only writer that enforces the "never stamp `last_verified_at` at write time" rule (`:888-891`); a direct `put` bypasses it |
| `remove` | `vaws machine remove` | `_workflow_common.remove_machine_record:932-969` |
| `get` | resolved target output / `vaws machine list` | `:457-460` returns the record that `resolve_remote_target` (`vaws_remote_toolbox.py:393-411`) and `machine_summary` (`_workflow_common.py:322-342`) already surface |

### 2.5 Maturity gap worth recording

CI (`.github/workflows/skill-catalog.yml:55-68`) runs the
`session-management` and `machine-management` test suites and the shared
scaffold/inventory regressions, and `.github/workflows/remote-dev.yml` covers
the substrate. It does **not** run `repo-init` tests (its test file sits at the
package root as `test_workspace_identity.py`, outside a `tests/` directory) or
`npu-fleet-monitor/tests/`. Both are cheap, hardware-free suites; wiring them in
is a prerequisite for calling those two packages "matured through heavy
testing".

## 3. Target resolution map

### 3.1 Every path from an argument to an endpoint

| # | Argument shape | Implementation | Evidence | Result |
| --- | --- | --- | --- | --- |
| P1 | `--machine <alias\|ip>` | `resolve_remote_target` legacy branch | `vaws_remote_toolbox.py:393-411` | container endpoint + host endpoint, `mode="legacy"`, empty `leased_devices` |
| P2 | `--session-id` | `resolve_remote_target` → `load_session_lookup` index scan | `vaws_remote_toolbox.py:358-391`, `vaws_session_state.py:549-569` | container + host endpoint, `mode="session"`, leased devices |
| P3 | `--session-file <path>` | same, explicit path | `vaws_session_state.py:545-547` | as P2 |
| P4 | no selector → nearest worktree binding (cwd upward) | `find_session_binding` | `vaws_session_id.py:101-131`, `vaws_session_state.py:511-524` | as P2; walk bounded at the repo containing `.agents/lib` |
| P5 | no selector → repo-root `current-session.json` | `load_current_session_binding` | `vaws_session_id.py:90-91`, `vaws_session_state.py:521-523` | as P2 |
| P6 | `--host` + `--port` | `.remote-dev` direct endpoint | `.remote-dev/core/endpoint.py:89-108`, `:148-149` | direct endpoint, `kind="direct-endpoint"` |
| P7 | `--alias` from `endpoints.json` / `endpoints.local.json` | `.remote-dev` alias merge | `.remote-dev/core/endpoint.py:73-86`, `:150-157` | direct endpoint with `alias` |
| P8 | `--host` (bare-metal, coordination only) | `npu_coordination.resolve_target` | `npu_coordination.py:66-73` | host endpoint only, `mode="direct-host"` |
| P9 | pool binding / `runtime_id` | coordinator `runtime_checkout` → `binding["endpoint"]` | `vaws_task_client.py:129`, `:152-159` | host/port/user/root from the manager |

Nine argument shapes. They are served by **nine independent implementations**,
several of which re-derive endpoints from the same inventory record:

| Implementation | Evidence | Overlaps |
| --- | --- | --- |
| I1 `vaws_remote_toolbox.resolve_remote_target` | `:346-411` | canonical for P1–P5 |
| I2 `.remote-dev/core/endpoint.resolve_endpoint` | `:147-172` | P6, P7 natively; delegates P1–P5 to I1 at `:111-144`; P4/P5 again at `:165-166` |
| I3 `vllm-ascend-serving/_common.resolve_machine` + `container_endpoint` / `host_endpoint` | `_common.py:107-137` | own inventory read and own endpoint construction for P1 |
| I4 `session_create.load_machine` + `_workflow_common.host_target` | `session_create.py:88-98`, `_workflow_common.py:413-422` | third inventory read and endpoint construction for P1 |
| I5 `npu_coordination.resolve_target` | `npu_coordination.py:66-98` | adds P8; wraps I1 for P1–P3 |
| I6 `coordinator/backend.resolve_registration` | `backend.py:74-84` | fourth alias→endpoint construction, for runtime registration |
| I7 `vaws_task_client` binding endpoint | `vaws_task_client.py:152-159` | P9 |
| I8 `_container_endpoint` (strict) | `vaws_remote_toolbox.py:294-303` | requires `container.ssh_port` |
| I9 `container_endpoint_from_record` (lenient) | `vaws_remote_toolbox.py:305-332` | tolerates a bare-IP `host` and falls back to the *host* port when `container.ssh_port` is absent — a silently different endpoint from I8 for the same record |

I8/I9 living in the same module is the sharpest instance of the problem: two
functions, one strict and one lenient, both claiming to answer "what is this
record's container endpoint", and the lenient one silently returns a host
endpoint when the container port is missing.

### 3.2 Genuinely different capabilities vs one mechanic in three coats

Three genuinely different capabilities:

1. **Direct endpoint** (P6, P7) — an address the caller already knows. No
   inventory, no session, no leases.
2. **Session target** (P2–P5) — an owned container plus leases, state namespace
   and worktree binding. P2, P3, P4 and P5 are *one* capability with four
   spellings; the precedence between them is already implemented once in
   `vaws_session_state.py:511-524` and `vaws_session_id.py:216-249`.
3. **Pool binding** (P9) — an exclusive runtime checked out from the manager,
   whose endpoint is the manager's to hand out.

Two that are one mechanic wearing extra interfaces:

- **Machine alias as an execution target** (P1) is inventory lookup, not a
  distinct capability. It is the same container endpoint a session would
  resolve, minus the lease and state isolation — which is exactly why
  `session-management/SKILL.md:87` restricts domain commands to sessions and
  `vaws_remote_toolbox.py:835-839` refuses serving state for non-session
  targets. It should survive as `vaws machine list/inspect` output feeding
  `session_create --machine` and `runtime_register --machine`, not as a
  `--machine` flag on execution commands.
- **Bare-metal `--host`** (P8) is a direct endpoint (P6) with a different flag
  name. `npu_coordination.py:66-73` builds its own `LocalEndpoint` class to do
  what `Endpoint` already does.

### 3.3 What should survive the split, and the compatibility period

Survivors: **4 argument shapes** (direct `host+port`; `alias`; one `session`
selector accepting id/file/binding with documented precedence; pool
`runtime_id`/binding) produced by **2 implementations** —
`.remote-dev/core/endpoint.resolve_endpoint` for the first three and the
coordinator binding for the fourth. I1 becomes an internal session-lookup
function called by I2, not a second resolver. I3, I4, I6, I8 and I9 collapse
into it.

The split makes this urgent in a specific way: `.remote-dev` is being extracted,
yet `.remote-dev/core/endpoint.py:111-121` reaches back into
`<repo>/.agents/lib` and imports `vaws_remote_toolbox.resolve_remote_target` by
path. After extraction that is a cross-repo import of a private function
(`_load_inventory` in `coordinator/backend.py:16` is the same problem — an
underscore-private function imported across what will be two repositories).
The direction of the dependency must be inverted before the split, not after:
the substrate should own the endpoint contract and accept an injected
session/inventory resolver, so the scaffold depends on the substrate and never
the reverse.

Compatibility period, given that `AGENTS.md` names three specific scripts as
legacy-`--machine`-compatible (`remote-code-parity/scripts/parity_sync.py`,
`session-management/scripts/npu_coordination.py`,
`vllm-ascend-serving/scripts/serve_probe_npus.py`) and that
`session_create.py --machine` is required (`session_create.py:420`):

1. **Freeze the list.** Those four are the entire `--machine` surface —
   confirmed by exhaustive grep of `.agents/skills`: the only other
   `--machine` parsers are `machine_*.py` (registration, correct),
   `inventory.py` (store), `remote_toolbox_stress.py:43` (validation harness)
   and the 18 `remote_*.py` wrappers via `add_target_args`
   (`vaws_remote_toolbox.py:1949-1953`). No new `--machine` flag anywhere.
2. **Keep the flag, change the resolver.** During the period, `--machine` stays
   accepted but resolves through the single surviving resolver, and every
   result's `target` block keeps reporting `mode: legacy` so a reader can tell
   an unleased legacy target from a session (`vaws_remote_toolbox.py:164-182`
   already does this).
3. **Emit a deprecation warning on `stderr`, never in `stdout` JSON.** The
   family's own contract is one JSON object on `stdout`
   (`remote-toolbox/SKILL.md:28-29`); a warning inside the payload would break
   consumers.
4. **Delete the 15 redundant `remote_*.py` wrappers only after the substrate
   repo's tag is pinned**, because those wrappers are the current fallback when
   an MCP client cannot load the substrate — `CLIENT_COMPATIBILITY.md:20-22`
   explicitly treats a CLI fallback as a distinct path, and
   `known-failure-signatures.yaml` records "fall back to direct `ssh`" as the
   response to an MCP tool-service fault. Removing them before the substrate is
   independently versioned would delete the escape hatch.

## 4. Device authority check

Target state: the host queue (`vaws_npu_coordination.py`, per
`coordinator/README.md:10-17`) is the sole allocation authority; the fleet
monitor is observation-only.

### 4.1 The monitor is clean — and here is how I checked

- `rg` for `vaws-top|vaws_top|VAWS_TOP` across the repository matches only
  `README.md`, `README.en.md`, `docs/npu-fleet-monitor.md`, and the monitor
  skill's own files. No `.agents/` skill, no `.agents/lib` module, no
  `.agents/coordinator` file and no `.remote-dev` module references it.
- `manage_monitor.py` reaches the monitor over HTTP exactly once, for
  liveness: `DEFAULT_URL = "http://127.0.0.1:8789/api/health"`
  (`manage_monitor.py:17`) used by `health()` (`:191-204`). It reads
  `status == "ok"` and nothing else — no device lists, no capacity.
- The monitor's own CLI (`servers`, `capacity`, `status`, `npu`, `mounts`) is
  documented as cached observation with "Capacity is observed availability, not
  a reservation" (`npu-fleet-monitor/SKILL.md:37`) and lives on a separate
  branch; no scaffold code invokes it.

**Result: no code path allocates from monitor observations.**

### 4.2 The coordinator is clean

`coordinator/backend.py` uses the inventory only as a *directory*
(`catalog:66-72`, `resolve_registration:74-84`) and routes every device
decision to the host queue: `host()` (`backend.py:87-94`) executes
`npu_coordination.build_remote_command(request)` on the host and refuses to
interpret `failed`/`needs_input`/`probe_failed` payloads locally. Grants,
fences, preflight and release all happen in `vaws_npu_coordination.py` on the
physical host.

### 4.3 One path does allocate from an observation — `session_create --npu-count`

This is the finding. Three lines of evidence:

1. `session_create.py:101-111` — `parse_host_npu_devices` calls the
   coordinator's parser but keeps **only** the `devices` key:

   ```python
   # session_create.py:101-111
   def parse_host_npu_devices(stdout: str) -> list[int]:
       """Visible device ids = chip-level Phy-IDs. ..."""
       from vaws_npu_coordination import parse_npu_smi_info  # noqa: PLC0415

       return parse_npu_smi_info(stdout).get("devices") or []
   ```

   The `busy`, `free` and `hbm` keys the parser computes
   (`vaws_npu_coordination.py:270-278`) are discarded.

2. `vaws_npu_coordination.py:252-254` — when the process table is missing or
   unparsable the parser fails **closed** for occupancy but still returns the
   device list:

   ```python
   # vaws_npu_coordination.py:252-254
   if process_error or not in_process_table:
       return {"status": "failed", "error": process_error or "npu-smi process table is missing",
               "devices": sorted(dev_ids), "busy": {}, "free": []}
   ```

   Because `parse_host_npu_devices` ignores `status`, a fail-closed occupancy
   parse is consumed as a successful device enumeration.

3. `vaws_session_state.py:322-339` — `--npu-count` then picks the first N
   devices that are unclaimed **in the workspace-local lease file**:

   ```python
   # vaws_session_state.py:332-339
   candidates = sorted(available_set)
   for dev in candidates:
       if _resource_owner(bucket, "npu_devices", str(dev)) in {None, sid}:
           allocated_devices.append(dev)
       if len(allocated_devices) >= npu_count:
           break
   if len(allocated_devices) < npu_count:
       raise SessionStateError(f"not enough locally unleased NPU devices for session {sid}")
   ```

   `load_leases`/`save_leases` read and write `.vaws-local/sessions/leases.json`
   under the current worktree (`vaws_session_state.py:115-124`), which
   `coordinator/README.md:25-27` calls "a compatibility mechanism, not a
   cross-workspace allocator". The host queue is never contacted:
   `session_create.py` has no import of `vaws_npu_coordination` other than the
   parser at `:108`.

Consequence, stated precisely: `session_create --machine <m> --npu-count 2`
can lease two cards that another workspace, another clone, or a human process
is actively using, and the error text ("not enough locally unleased NPU
devices") names the wrong authority. `--devices` is safer only in that the
agent chose the cards; `vaws_session_state.py:313-321` validates them against
the same visibility observation, again without occupancy.

Two mitigations already exist and are worth crediting, because they bound the
blast radius:

- **Serving refuses to allocate.** `require_session_npu_lease`
  (`vaws_session_state.py:424-434`) rejects empty or stale lease snapshots, so
  a service cannot fall through to idle-card selection —
  `session-management/SKILL.md:84` states this and `serve_start.py:58` imports
  it. Serving consumes the lease; it never picks cards.
- **Release is proof-gated.** `session_gc._probe_session_container`
  (`session_gc.py:173-219`) requires host Docker state plus
  `_confirmed_free_probe(samples=2, …)` before reaping, and
  `session_remove.py:206-220` refuses lease release without confirmed container
  removal.

So the family is fail-closed on *release* and fail-open on *acquire*. The fix
is one call site: `allocate_session_leases` should take the host queue's grant
(or, at minimum, refuse to allocate when `parse_npu_smi_info` returned
`status != "ok"` and when the requested devices appear in `busy`).

## 5. The two opposite errors

### 5.1 Scripting a decision the agent should make

**The image gate is the model.** `.agents/README.md:73` makes image selection an
explicit user decision, and the code honours it end to end:
`resolve_workflow_image` returns a `needs_input` payload rather than a value
when no image was chosen (`_workflow_common.py:232-238`), refuses to reuse a
recorded image that is missing, `auto`, or a `:latest`-style moving tag
(`:240-248` with `image_requires_explicit_reselection:210-221`), and the
payload enumerates the five options with resolution semantics and a
`forbidden_defaults` list (`:152-207`). Both low-level subcommands also mark
`--image` `required=True` (`manage_machine.py:3281-3287`, `:3358-3364`). This is
what a scripted mechanic plus an agent decision should look like.

Three decisions did **not** get the same care:

1. **Session containers inherit the base image with no reselection gate.**

   ```python
   # session_create.py:532
           image = args.image or base_record["container"]["image"]
   ```

   `session_create.py:532` bypasses `resolve_workflow_image` entirely. If the
   inventory record holds `auto`, a bare repository, or a `:latest` tag — the
   exact three cases `_workflow_common.py:210-221` rejects — the session
   container is bootstrapped from it silently. `machine_add` would have stopped
   and asked; `session_create` does not. Same file, same helper module already
   imported (`session_create.py:27`).

2. **The prepared-image cache is on by default and changes runtime identity.**
   `session_create.py:674` passes `use_prepared_image_cache=not
   args.disable_prepared_image_cache`, so the container is started from a
   host-committed derivative (`vaws-session-prepared:<hash>-ssh-v2`,
   `manage_machine.py:1749-1753`) rather than the selected image, and
   `manage_machine.py:2306-2311` will even adopt an existing container's
   recorded base image. The image the user selected and the image that runs are
   different objects; `session-management/SKILL.md:161` documents the speedup
   but the decision is opt-out, not asked. `machine-management/SKILL.md:232`
   deliberately keeps managed-base bootstrap on raw images — the asymmetry is
   intentional for speed, but it is a runtime-identity decision made by a flag
   default.

3. **`--verification-mode` defaults to `ssh`.** `session_create.py:441-446`
   defaults to skipping the `torch`/`torch_npu` smoke, so a session reports
   `ready` on SSH plus device-visibility only
   (`verify_session_ssh:211-311`). Defensible and documented
   (`SKILL.md:163`), but it is the script deciding how much proof "ready"
   requires — the same class of decision the image gate refuses to make.

A latent fourth: `DEFAULT_IMAGE = IMAGE_SELECTOR_RC`
(`manage_machine.py:66`) still exists as a constant. Nothing consumes it today
(the only default in use is `DEFAULT_IMAGE_CANDIDATES` inside help text at
`:3285`), but a constant named `DEFAULT_IMAGE` in a module whose contract is
"never default the image" is an invitation.

### 5.2 Leaving mechanics to the agent that should be pinned

1. **The container-hostname mapping is not enforced at bootstrap.** The
   verified fix for the gloo signature is `127.0.0.1 <hostname>` in the
   container's `/etc/hosts`, and the knowledge entry says session bootstrap
   should enforce it on **every new container**
   (`known-failure-signatures.yaml:22`). Exhaustive grep for `etc/hosts` finds
   it implemented in exactly one place: `serve_start.py:169-179` (serving
   family). Neither `manage_machine.render_bootstrap_host_script`
   (`manage_machine.py:1561+`) nor `session_create.py` writes it. Any
   distributed launch that does not go through `serve_start` — a `remote.bash`
   `torchrun`, operator debug, a PD-serving path — re-hits a signature the
   repository has already paid for. This is a pinned mechanic left to whoever
   remembers.

2. **No `machine list`.** Every public machine verb requires `--machine`
   (`machine_verify.py:28`, `machine_repair.py:42`, `machine_remove.py:31`), so
   discovering what is registered means reaching into the low-level store
   (`inventory.py:454`) — the exact "do not start with the low-level helpers"
   the skill forbids (`machine-management/SKILL.md:93`). The mechanic exists;
   only the entry point is missing, which pushes agents to the wrong surface.

3. **No `session logs` / service-log verb in the owning family.** The only log
   tail is `remote_service_logs` (`vaws_remote_toolbox.py:1799-1833`), reached
   through the compatibility layer. An agent following
   `session-management/SKILL.md` finds no way to read its own service logs.

4. **Mesh repair has no wrapper.** `sync_mesh` failure downgrades `machine_add`
   to `needs_repair` (`machine_add.py:356-372`) with the message that
   cross-machine operations will not work "until repaired" — but the only
   repair path is re-running the whole `machine_repair` flow or hand-driving
   `manage_machine mesh-add-peer`. A deterministic, idempotent
   `vaws machine mesh --repair` is missing, so the recovery mechanic lands on
   the agent.

5. **`--session-id` uniqueness for jobs is delegated with a warning instead of
   removed.** `remote-toolbox/SKILL.md:32-35` tells the agent that an explicit
   `--job-id` must be "globally unique in this workspace"; the code does block
   duplicates (`vaws_remote_toolbox.py:924-933`), so the guidance is
   unnecessary — generated ids (`:920-923`) are the pinned mechanic and the
   flag mostly exists to let an agent get it wrong.

## 6. Diagnosability gaps

Cross-checked against `.agents/knowledge/known-failure-signatures.yaml`. The
recorded distinction — *"Treat instant 'timeout' from MCP `remote_bash` as a
tool-service fault signature, not a remote command fault"*
(`known-failure-signatures.yaml:52`) — is not representable in any result
contract in this family.

### 6.1 No layer field in the result contract

`remote-dev.result.v1` has `tool`, `target`, `outcome`, `status`, `summary`,
`refs`, `warnings` — and no field naming the failing layer
(`.remote-dev/core/result.py:20-57`). `remote_bash` sets
`outcome=status="timeout"` from a single boolean
(`.remote-dev/core/shell_ops.py:137-140`) whose only source is
`subprocess.TimeoutExpired` in the local client
(`.remote-dev/core/ssh_transport.py:100-103`). A local tool-service fault, a
dead TCP path, a wedged container and a genuinely slow command all render
identically. `duration_ms` is present (`shell_ops.py:164`), so the "instant"
part of the recorded signature is *inferable* — but nothing in the payload,
the `summary`, or the `warnings` says so, and the exception classes that exist
(`RemoteDevError`, `EndpointError`, `PathPolicyError`,
`RemoteExecutionError` — `.remote-dev/core/errors.py:4-17`) are never mapped
into a layer attribute. Minimum fix that changes no behaviour: add
`failed_layer` ∈ {`local-tool`, `transport`, `container`, `host`} plus, for
timeouts, `timeout_source` ∈ {`local-deadline`, `remote-timeout-command`}.

### 6.2 `rc=255` conflates a local timeout with a transport failure

```python
# vaws_remote_toolbox.py:470-475
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        result = subprocess.CompletedProcess(
            cmd, 255, "", f"ssh_exec timed out after {exc.timeout}s"
        )
```

255 is the exit code OpenSSH itself uses for connection failure. The docstring
even notes a local timeout is "same shape as a lost SSH connection" — but the
two are different layers, and the caller now cannot tell them apart except by
string-matching stderr.

### 6.3 The verified mux fix does not exist on the substrate

The recorded resolution is `base_ssh_options(mux=False)` for streams and
tunnels (`known-failure-signatures.yaml:51`), implemented in
`.agents/lib/vaws_ssh.py:64-85`. `.remote-dev/core/ssh_transport.py` has no
equivalent: `_control_master_options` (`:30-64`) is applied unconditionally by
`ssh_base_cmd` (`:67-83`) for `run_script`, `run_bytes` and
`run_remote_python`. So the substrate that `.agents/README.md:190` calls the
preferred surface still carries the failure mode the family already diagnosed
and fixed elsewhere. Worse for attribution: when the mux directory cannot be
prepared, the degradation is announced only on `stderr`
(`ssh_transport.py:43-48`), which an MCP client does not surface to the model —
the same warning in `vaws_ssh.py:40-45` reaches a CLI user but not an MCP one.

### 6.4 Resolution failures lose the selector

```python
# vaws_remote_toolbox.py:1964-1976
def _cli_error(exc: BaseException, *, started_at: str, start: float) -> int:
    status = "failed"
    if isinstance(exc, (RemoteToolboxError, WorkspaceStateError, ValidationError, FileNotFoundError)):
        status = "needs_input"
    ...
    print_json({
        "status": status,
        ...
        "error": str(exc),
        "target": None,
```

Every wrapper funnels errors here, and `target` is hard-`None`. Given nine
argument shapes (§3.1), the one fact needed to debug a resolution failure —
which selector was tried, and which of inventory / session index / worktree
binding answered — is dropped. `load_session_lookup` writes good messages
(`vaws_session_state.py:579-589`) but only as a string, and its "index is
corrupted, trying the next candidate" degradation goes to `stderr`
(`vaws_session_state.py:556-560`) where JSON consumers lose it.

### 6.5 Probe status does not name the failing plane

`probe_remote` gathers host facts, container facts, service state and
`known_hosts` facts for both planes — genuinely good raw material
(`vaws_remote_toolbox.py:812-830`). But the top-level status is
`"ok" if container_facts.get("status") == "ok" else "needs_repair"`
(`:813`), so an unreachable **host** and a broken **container runtime** produce
the same verdict, and the caller must dig into two nested payloads to tell
which. The layer information is present and then thrown away at the summary
line.

### 6.6 Job status collapses two distinct causes

`remote_job_status` maps a missing/unreadable remote `status.json` to
`unknown` → `needs_repair` (`vaws_remote_toolbox.py:1067-1071`). A deleted job
directory, a container that lost its filesystem, and an SSH failure are
indistinguishable — which is exactly the wedge the coordinator documents as
requiring explicit operator reconciliation
(`coordinator/README.md:433-444`). The toolbox path has no equivalent of that
"never infer completion from a lost directory" attribution.

### 6.7 Catch-all handlers erase the phase

`machine_add.py:398-403` and `machine_repair.py:327-332` both end in

```python
# machine_add.py:398-403
    except WorkflowError as exc:
        print_json({"success": False, "status": "blocked", "action": "failed", "error": str(exc)})
        return 2
    except Exception as exc:  # noqa: BLE001
        print_json({"success": False, "status": "blocked", "action": "failed", "error": str(exc)})
        return 2
```

`action: "failed"` for a flow with eleven named phases. The phases were emitted
on `stderr` (`_workflow_common.emit_progress:69-79`), so the information exists
at runtime and is absent from the artifact an agent keeps.
`session_create.py:788-800` has the same shape: `{"status": "failed", "error":
…}` with no phase, no lease state and no worktree state, even though the run
may have created a worktree, allocated leases and bootstrapped a container
before failing. `npu_coordination.py:383-385` likewise drops the resolved
target on the generic exception path — while its non-JSON path
(`:363-376`) does the right thing and includes returncode plus both stream
tails.

### 6.8 A hygiene note that belongs here

`remote-toolbox/references/command-recipes.md` and
`references/stress-validation.md` hard-code two real host addresses in every
example, and `.remote-dev/README.md`'s validator example does the same. The
knowledge base entry for the gloo signature also carries a host range in its
`applicable_versions` field (`known-failure-signatures.yaml:10`). Tracked
example commands should use placeholders; this is the mechanism by which an
internal address range reached public history before, and it is also a
diagnosability issue in reverse — a recipe pinned to two specific hosts stops
being runnable evidence for anyone else.

## 7. Proposed consolidation

Target surface for this family: **5 entry points, 28 verbs** (from 39 files and
73 verbs), plus the monitor's 4 verbs in its own repository.

| Entry point | Verbs | Absorbs |
| --- | --- | --- |
| `vaws machine` | `add`, `verify`, `repair`, `remove`, `list`, `mesh` (6) | `machine_*.py` ×4, `manage_machine.py` ×10 (becomes a library), `inventory.py` ×5 (becomes a store), adds the missing `list` and `mesh` |
| `vaws session` | `create`, `list`, `status`, `diff`, `remove`, `gc`, `group` (7) | the eight session scripts, unchanged in behaviour |
| `vaws npu` | `submit`, `acquire`, `preflight`, `activate`, `heartbeat`, `release`, `cancel`, `status`, `gc`, `hold` (10) | `npu_coordination.py`; **moves to the coordinator repository**, since `coordinator/backend.py:19-21` already treats it as the host authority |
| `vaws cleanup` | (1) | `remote_cleanup.py` |
| `repo-init` | `probe`, `profile`, `topology`, `ci-pin`, `install-gh` (5) | the five repo-init scripts and the PowerShell twin |
| *(other families)* | `serve logs`, `parity plan/apply` | `remote_service_logs` → serving; `remote_sync_plan/apply` → remote-code-parity |
| *(substrate)* | `remote.*` | the 15 redundant `remote_*.py` wrappers, after the substrate repo is pinned |
| *(tests)* | — | `remote_toolbox_stress.py` moves under `.agents/tests/` |

Sequencing, because three subsystems are being extracted concurrently:

1. **Invert the `.remote-dev` → `.agents` dependency** (`endpoint.py:111-121`,
   `backend.py:16`). This is the only item that becomes materially harder after
   the split rather than merely untidy.
2. **Collapse the nine resolver implementations to one** (§3.3), keeping all
   four surviving argument shapes accepted.
3. **Fix the acquire path's authority** (§4.3) — one call site.
4. **Add `failed_layer` to the result contract** (§6.1) before the substrate is
   versioned separately, since it is a schema change.
5. **Retire the 15 redundant wrappers** last.

The most urgent duplication is **target resolution**, and specifically the
cross-repo direction of `.remote-dev/core/endpoint.py:111-121`. Every other
redundancy in this audit is a surface that costs agent accuracy today and can
be deleted at leisure; that one becomes a versioned contract between two
repositories the moment the extraction lands, with a private function
(`resolve_remote_target`, and `_load_inventory` for the coordinator) on the
wrong side of the boundary.
