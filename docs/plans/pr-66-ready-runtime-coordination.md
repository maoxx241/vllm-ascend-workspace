# PR #66: ready runtimes and resource coordination

Status: implemented initial shared-runtime path, 2026-08-28; hardware rollout remains pending. Checkboxes describe delivered and
verified work, not capabilities inferred from this document.

## Agreed outcome

An agent edits its actual local business worktree and uses remote-dev to work
in an already prepared, isolated remote runtime. A logical session does not
own a machine or NPU for its entire lifetime. A shared Streamable HTTP MCP
service supplies runtime bindings and coordinates resources per execution.
The monitor on `vaws-top` is optional and is not a dependency.

Development normally changes code and **restarts the model service**. Reuse
containers, compatible environments, native artifacts, and on-disk weights;
do not build a resident model-service pool. Loading weights and capturing a
new graph remain execution costs and are not hidden in an environment-ready
claim.

For a compatible ready-runtime hit, the request path must perform zero
container creations, zero dependency installations, and zero unnecessary
native rebuilds. An unavailable profile or artifact is an explicit wait/miss,
not permission to silently provision during a launch.

## Why: observed K3 failures

The owner's 2026-08-27 historical audit classified 82 independent service-start
failures: 21 environment/deployment, 11 resource/residual-process, 9
communication/port, 4 launcher, 5 capacity, 22 model/operator, and 10
graph/KV/parallelism. Nine additional deployment/build failures were tracked
separately. There is no total-attempt denominator and no measured avoidance
rate. These historical observations do not establish current defects.

Concrete requirements derived from that audit:

| Failure | Required behavior |
| --- | --- |
| Missing `VLLM_VERSION` or model plugin registration | Record and validate the complete runtime/launch profile, not just an image tag. |
| `PYTHONPATH` replaced and `acl`/native compatibility imports lost | Preserve the base environment and append scoped source paths. |
| Missing kernel library, `binary_info_config.json`, or vendor metadata | Treat native artifacts as complete, fingerprinted bundles. |
| Stale absolute-path CMake cache or omitted submodule inputs | Fingerprint actual build inputs; do not copy arbitrary build directories. |
| Unrelated HEAD movement triggers repeated rebuilding | Compare native/dependency inputs, including committed changes and reversions. |
| Old workers or another session still own cards | Keep leases on failed probes/cleanup; require live ownership before launch. |
| Wrong node/interface/port or mismatched multi-node environments | Record topology and per-node versions; do not infer four-node acceptance from one node. |

## Architecture and invariants

- **Session:** stable task identity, actual source worktrees, and user constraints.
- **Environment profile:** immutable version/build identity for CANN,
  torch/torch_npu, Python ABI, vLLM, vllm-ascend, operators, SoC and driver
  compatibility. Profiles are verified combinations, not independent `latest`
  selections. No K3 version combination is declared verified without evidence.
- **Runtime binding:** exclusive use of an already prepared container and its
  writable source area. No NPU reservation is implied by environment checkout.
- **Execution lease:** actual host/device allocation, ownership and fencing
  token, activation, heartbeat, and confirmed release.
- **Snapshot and artifacts:** a run fixes the source snapshot and native bundle
  identity. Later edits go to staging and must not change a running experiment.

Keep the existing remote-dev tool names and endpoint parameters. The new MCP
surface handles resource intent and binding; it does not replace remote
read/edit/bash/job/artifact tools. All participating clients share one authority
for each resource; do not add competing workspace-local and service-local NPU
allocators. Existing advisory/manual entry points are explicitly outside hard
hardware enforcement.

Human allocation through agent conversation becomes an attributed constraint,
reservation, or cooperative yield request. Silence, timeout, a lost SSH
connection, or an accepted yield message never proves that devices are free.
Do not kill another task automatically. MCP events need a polling/listening
client and do not automatically wake a paused agent.

## Implementation sequence

### 1. Repair the current PR's safety boundaries

- [x] Do not classify container SSH failure as proof of container death.
- [x] Keep leases when metadata is unreadable or remote cleanup is unproven.
- [x] Do not release leases merely because local worktree removal succeeded.
- [x] Preserve branches, commits, and dirty child worktrees when reusing a session.
- [x] Require a nonempty live NPU lease for managed serving, including restart.

### 2. Make source synchronization and native reuse precise

- [x] Record build-input fingerprints separately from complete source snapshots.
- [x] Reuse native installs for Python-only commits as well as uncommitted edits.
- [x] Invalidate on native/dependency changes, reversions, environment changes,
      and missing artifacts; preserve vLLM-to-Ascend dependency invalidation.
- [x] Supply complete artifact/profile manifests and an explicit cache-miss path.
- [x] Add a local incremental-sync loop using existing parity/remote-dev
      transport; edits target staging and launch pins the selected snapshot.

### 3. Add the independent coordinator and ready-runtime path

- [x] Add a shared, authenticated Streamable HTTP MCP entry point with persistent
      runtime/binding/event state and a bounded background reconciliation loop.
- [x] Register/adopt prepared runtimes, match exact environment profiles, and
      atomically check out an exclusive runtime without Docker/build operations.
- [x] Reuse host-side NPU coordination for requests, preflight, activation,
      heartbeat and release; expose queue/yield/reply/poll operations.
- [x] Return remote-dev endpoints and explicit session/run ownership rather
      than binding a logical session permanently to one host.
- [x] Reconcile restart, expired requests, uncertain remote state, and return
      to the pool conservatively. Persist state outside `/tmp` on the manager.
- [x] Keep first-time preparation and pool replenishment outside launch; reuse
      existing preparation tools rather than introducing a second installer.

### 4. Verification and PR publication

- [x] Extend existing lease/worktree/parity tests and add focused coordinator
      integration tests for two clients and competing requests.
- [x] Verify real MCP calls, authorization, restart persistence, queue progress,
      event cursors and the zero-provisioning checkout path in CI/Linux.
- [x] Run regression tests on Linux/remote containers; do not execute
      torch/torch_npu workloads on a local Mac.
- [ ] Publish exact checks and remaining hardware/client validation in PR #66.
- [ ] Hardware acceptance: two isolated code states, disjoint devices/ports,
      real requests, release/restart of one without affecting the other.
- [ ] K3 acceptance is a separate exact-topology run with all-rank evidence and
      a real request; environment import checks are not model acceptance.

## Non-goals for this change

- Resident model services solely to avoid development restarts.
- Transparent migration of a running model process between machines.
- Modifying shared host drivers/CANN or replacing another task's container.
- Rebuilding every environment/version combination proactively.
- A new monitor UI, a second SSH toolbox, or duplicated business repositories
  kept aligned by rebase.

## Evidence ledger

Initial PR head: `065c99fff39b82491892f89e063eceb322a148d2`.
The original PR reports a two-session hardware run; this update has not yet
independently reproduced that report. Record new validation here as it runs.


## Integration of merged PR #67

Merged main `271c57f96e3ed25cf394f8571aad723bc0754fcd` into this branch,
resolving the three inventory conflicts by behavior rather than whole-file
selection. Preserve `shared_workspace_root/shared_inventory_path` and inventory
scope metadata. Missing shared inventory remains an explicit execution error;
legacy/per-worktree files are not implicit migration inputs. Old data can be
inspected with an explicitly selected inventory path before a deliberate
migration. Independent clones do not share a Git common-dir directory.

Parity machine-mode inventory now uses that same shared resolver. Its tests
cover stale local inventory, missing shared inventory, independent clones,
and separate session roots. Session-id lookup does not use this machine loader.
The coordinator can resolve registration through this machine directory while
keeping runtime checkout and actual NPU allocation under their shared authorities.
Tests use two principals with distinct management/source roots, not just two
session ids in a single local lease file.

## Initial delivery boundary

The shared manager, authenticated HTTP MCP calls, prepared-runtime registration,
checkout/return/refresh, declared TCP ports, host queue/fences, durable messages,
external-worktree staging and native/profile manifests are implemented. It uses
existing remote-dev and host coordination rather than a second SSH/NPU stack.
Native bundle reuse is restricted to matching inputs, environment and installation
path; relocation is not assumed. On the warm pool path use materialize, not a
legacy automatic first install. A cache miss is explicit and preparation occurs
separately.

The first version has no multi-host atomic/gang allocation or automatic pool
replenishment. Legacy domain wrappers are not transparent pool-binding adapters;
use the documented coordinator plus remote-dev lifecycle. Direct/manual access
can bypass cooperative scheduling and is not claimed to be hard isolation.

No actual machine, CANN stack or model service was modified during this update.
The old registered container endpoint refused connection; other observed host
containers were not used. Real prepared-slot, dual-service, five-client product
and exact four-node K3 validation remain outstanding. No version combination
has been marked K3-compatible solely from CI or an image name.

### New verification evidence

- `ed874db`: safety/parity fixes passed Skill catalog and Remote-dev contracts.
- `d21398d`: shared-inventory integration and actual two-client HTTP MCP tests
  passed (Skill catalog run 33139603977, Remote-dev run 33139603969).
- `e526d8d`: CI caught a Run Manifest v1 top-level field mismatch; this was not
  a passing release. Fixed by placing coordination data under environment.
- `1d8a6be`: both workflows passed again, including lost-grant/submit reply
  recovery, Run Manifest v1 exports and external-source staging tests (Skill
  catalog run 33140143240, Remote-dev run 33140143182).
- `6991d7e`: all four checks passed after documentation, declared-port guards
  and expiry recovery updates (Skill catalog run 33140577672, Remote-dev run
  33140577671). Logs confirm 13 coordinator/HTTP tests, 32 session tests,
  17 scaffold safety tests, 5 shared-inventory tests and 12 parity tests.
- Final-head verification is recorded in the PR description, including the
  evidence-refresh cache regression added after this passing snapshot.
