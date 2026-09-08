# Repository boundaries

Status: current

This file is the **current consumer-side contract** for the scaffold after
the three extractions landed: what this tree consumes from remote-dev,
vaws-coordinator, and vaws-top, and which ownership rules later work must
preserve. The dated 2026-09-07 inventory, 71-row table, full guard write-up,
and historical sequenced plan live in
[audits/repo-boundaries-2026-09-07.md](audits/repo-boundaries-2026-09-07.md)
and must not be followed as direction. The machine-checkable policy is
[`.agents/policy/repo-boundaries.json`](../.agents/policy/repo-boundaries.json);
the checker is
[`.agents/scripts/repo_boundary_check.py`](../.agents/scripts/repo_boundary_check.py).

## The guard (current mechanism)

`.agents/scripts/repo_boundary_check.py` reads that policy, scans tracked
Python, and reports forbidden cross-subsystem references. Bounded progress
on `stderr`, one JSON payload on `stdout`.

```bash
python3 .agents/scripts/repo_boundary_check.py --mode report
python3 .agents/scripts/repo_boundary_check.py --mode enforce
```

Exit codes: `0` clean or `--mode report`, `1` policy violated, `2` unusable
policy/baseline/invocation. The baseline
(`.agents/policy/repo-boundaries-baseline.json`) records accepted hits with
three anti-rot properties: nothing new passes, a fixed hit must delete its
row, and every row is attributed (`unassigned` fails enforce). Fingerprints
are `rule|path|symbol` with no line number. The full 2026-09-07 write-up of
this mechanism is in the [dated snapshot](audits/repo-boundaries-2026-09-07.md).

## Current contract

A reader following current migration directions must reach the already
accepted task-provider, worker, and host-authority ownership, and must
**never reinstall task dispatch into remote-dev**.

This combined tree consumes remote-dev through the #90 external checkout,
launcher (`.agents/scripts/remote_dev.py`), and resolver
(`.agents/lib/vaws_remote_dev_plugin.py`). Tracked `.remote-dev` is gone.
The pin is `62045af1f76c803ca392ae413b56bcfe290e6450`. That is a statement
about this source, not a claim that this PR has already merged publicly, and
not a runtime or hardware qualification.

The coordinator consumer now **exists** in this combined tree: pin, locator,
launcher, dual-provider client setup, owned-hook preservation, and residual
compatibility adapters. Arrival evidence and the deleted in-tree writers are
in [coordinator-consumption.md](coordinator-consumption.md). The pin is
`a7d5005a4df6ab8adf5b16a965127e81a30ee3fc`. Task tools, registry writes, and
the managed supervisor are not reimplemented here.

The first-stage vaws-top consumer now **exists** in this combined tree:
`npu-fleet-monitor` locates the published repository instead of a scaffold
`vaws-top` branch worktree. The pin is
`e7af28e629e7fd79c47e9b096f1dc1fd94f665ab`. vaws-top is published in its
canonical repository but private; published is not public, and this
integration does not change visibility. That is pinned source consumption,
not a runtime, client, or NPU deployment.

The SHAs below are source implementation facts from independent
acceptance. The original 71-row audit dated `2026-09-07`
remains explicitly dated historical evidence in [audits/repo-boundaries-2026-09-07.md](audits/repo-boundaries-2026-09-07.md) of
`605a7746a34f88c8235b56505060ecd937cb77df`; they are not a census of this
current tree.

### Source pins (implementation facts, not deployment)

| Repository | Role | Exact SHA |
|---|---|---|
| `vaws-coordinator` | accepted actual main after independent #1/#2/#4 | `a7d5005a4df6ab8adf5b16a965127e81a30ee3fc` |
| `remote-dev` | ledger + glob + mux accepted actual provider main | `62045af1f76c803ca392ae413b56bcfe290e6450` |
| `vaws-top` | independently inspected standalone main consumed by this first-stage locator | `e7af28e629e7fd79c47e9b096f1dc1fd94f665ab` |

Independent coordinator #1 (`84cb6bdd01a2eb5afedd3e7216ace4cc7acc1285`) and
#2 (`91b8bf52d2ba92a7586d34e2536f167f9f8d583b`) are the reviewed blobs that
landed on that coordinator main. Their control-plane evidence is source,
protocol, and Linux process-supervision evidence only.

Accepted scaffold main at the post-transfer documentation dispatch includes
#90, #98, #91, #100, #84, and #85. That is this source tree, not installed
client, runtime, or hardware evidence.

### Current source repositories

Canonical clone/upstream for the scaffold is the organization repository.
The organization inventory is six repositories. The two business forks are
personal public repositories outside that inventory: source-plane inputs,
not extraction destinations and not deployment units. `.gitmodules` still
points at `vllm-project/vllm` and `vllm-project/vllm-ascend`; default-branch
SHAs below are identity facts, not submodule gitlink updates. remote-dev and
vaws-top numeric ids are retained from the dated 2026-09-07T13:13:24Z
metadata; current visibility comes from the organization inventory.

| Organization repository | Id | Visibility | Observed default-branch SHA | Responsibility |
|---|---:|---|---|---|
| `vllm-ascend-workspace/vllm-ascend-workspace` | 1196723340 | public, non-fork | `7af4ac3106649d2dbbed712c780a14db8bf25113` | canonical scaffold |
| `vllm-ascend-workspace/remote-dev` | 1360023179 | private | `62045af1f76c803ca392ae413b56bcfe290e6450` | transport and explicit endpoints |
| `vllm-ascend-workspace/vaws-coordinator` | 1360026044 | public | `a7d5005a4df6ab8adf5b16a965127e81a30ee3fc` | task/provider/pool protocol and managed worker |
| `vllm-ascend-workspace/vaws-knowledge` | 1359978527 | public | `1eac65cf2f8ff4f1451c788f0964005ea0dfdee2` | formal knowledge corpus and source identity |
| `vllm-ascend-workspace/vaws-top` | 1360023247 | private | `e7af28e629e7fd79c47e9b096f1dc1fd94f665ab` | fleet monitoring; observation only |
| `vllm-ascend-workspace/.github` | 1360014025 | public | `fc6a1929fc13b2844f47012a1dec296daff09936` | organization landing metadata, not a runtime provider |

| Personal source-plane fork | Id | Visibility | Observed default-branch SHA | Parent / source |
|---|---:|---|---|---|
| `maoxx241/vllm` | 1009465986 | public fork | `a435e3108d82eb96d9b3954c1935afbbf4c5f69b` | `vllm-project/vllm` (599547518) |
| `maoxx241/vllm-ascend` | 924147541 | public fork | `d52c1b8de956507e6ace7ba351a998ed5cee6ce5` | `vllm-project/vllm-ascend` (924058625) |

Source main SHA, scaffold dependency pin, accepted consumer wiring, installed
runtime, and hardware qualification remain distinct facts. A later default
branch does not by itself bump a pin or broaden validation. remote-dev issues
#1 and #2 remain open; code presence is not issue closure. The accepted mux
change isolates the deliberate-interruption path locally; it does not repair
every failure from killing an SSH ControlMaster, promise universal
cancellation isolation, or constitute a hardware replay.

`vaws-knowledge` Stage 2 #9 is on the accepted source main above. The
scaffold pin `.agents/deps/vaws-knowledge.json` is
`4208de3ca88f5146472353f23c5f5d216767bf47`, an external conformance **test
kit** selected through `VAWS_KNOWLEDGE_KIT_ROOT`, not a runtime dependency. Two scaffold v2 entries remain
unverified and export-blocked; nine model facts remain v1. Shared-cache
import is an explicit local
`.agents/scripts/knowledge_shared_cache.py import` and is not a periodic
automatic pull-back. Root has accepted all three bounded live runs. Preview
`34144173739` is a valid zero-eligible-input result (propose job skipped;
one controlled incomplete-scope refusal; parent plus 35 accessible forks =
36 observed repositories; fork-count discrepancy 1; no complete-universe
claim). Proposal `34145663787` is nothing-to-propose with `wrote=false`.
Snapshot `34145761264` published an empty verified-layer snapshot
(`entry_count=0`). Advisory is unavailable for "no eligible input" and
`provider.called=false`. Scope limits are retained. Consumer refresh, an
actual Grok/provider semantic review, a positive candidate proposal, and a
nonempty verified corpus were not run.

### Ownership that current work must preserve

**Task tools and the managed-job worker belong to the coordinator.** The
four-tool stdio provider is `task_server.py`, serving exactly `vaws_session`,
`vaws_run`, `vaws_execution`, and `vaws_finish` (official MCP SDK 2.1.1
tools/list; experimental `service_api_version=1`). The supervisor is
coordinator-owned `workers/managed_jobs.py`, recovered byte-identical from
historical remote-dev `900ad15^:core/managed_jobs.py` at SHA256
`f2960c7de21867205b02cb9faf11acfc2e78626b8e942eac4e3d759e355ec6f4`. Backend
`worker_source()` reads that coordinator-owned source text; the workers
directory is not inserted into manager import paths. remote-dev does not
supply the supervisor. Pool/task state and the supervisor remain
coordinator-owned.

**remote-dev is transport only.** It provides explicit endpoint operations
(`direct_endpoint` or `resolve_endpoint` from an explicit
`host`/`port`/`user`/`root`/`cwd` mapping, plus `remote_bash`) and the
consumer resolver extension. It does not own task tools, a managed-job
worker, or host allocation. A transport registration hook for foreign tools
is a dated historical proposal from the [2026-09-07 snapshot](audits/repo-boundaries-2026-09-07.md) and must not be implemented.

**Host NPU authority is one coordinator-owned implementation.**
`host/vaws_npu_coordination.py` lives in the pinned vaws-coordinator checkout.
The scaffold consumes that file through the same locator as every other
coordinator surface and does not keep a second copy. `VAWS_HOST_QUEUE_MODULE`
is an override the scaffold no longer sets; the coordinator defaults to its
bundled module. The published contract is still `handle_request` and
`CoordinationError`. There is one authoritative host state and no second
allocator. vaws-top observes fleet inventory and must not grant leases. The
host-protocol source is a published contract, not permission to restore
`session-management/scripts/npu_coordination.py` skill-script imports.
Independent #1 tests used the workflow pin
`161fed1b0fe6b48359be3f0cf33bb7d8befae113` and extracted authority digest
`d9e03cc0ef8a65f2ebc5081468dc3cc2fe079fad711349d51e8d6052b334fbff`.

**Dependency direction is coordinator → remote-dev.** A lower layer must not
import scaffold or coordinator task state. Cutting the historical remote-dev / coordinator cycle means moving
the task facade *out* of remote-dev, not teaching remote-dev to register it.
The coordinator reaches remote-dev only through explicit endpoints; it does
not ask the substrate to resolve an alias, session, or machine.

**State and dependency roots keep their installed identities.** The local
task registry location (`VAWS_AGENT_SESSIONS_DIR`; default remains this
installation's existing `agent-sessions` path) and the manager pool
(`--state-dir`, one database, no heuristic second manager) must be preserved
across consumer wiring. Pure schema and build-input mirrors may be pinned
byte-identical. Mutable task, lease, or host-queue writers may not be copied.

### Current source state

- Coordinator consumption by this scaffold **exists** in this combined tree
  (pin, dual-provider client setup, deletion of in-tree task writers after
  destination arrival evidence, owned-hook preservation, locator/launcher
  adapters). See [coordinator-consumption.md](coordinator-consumption.md).
- The first-stage vaws-top consumer **exists** in this combined tree.
  `npu-fleet-monitor` locates the published repository instead of a scaffold
  `vaws-top` branch worktree, and the four obsolete `vaws-top` baseline rows
  are removed. vaws-top is published in its canonical repository but
  private; do not equate published with public. This is offline pinned
  source consumption only, not public publication, a live dashboard
  deployment, client setup, or NPU allocation.
- Current boundary counts in `.agents/policy/repo-boundaries-baseline.json`
  are zero accepted, zero new, and zero stale.
- The original 71-row audit, 292-file scan, and 26 / 41 / 4 `removed_by`
  split remain dated historical evidence in
  [audits/repo-boundaries-2026-09-07.md](audits/repo-boundaries-2026-09-07.md).
  Do not rewrite the 71-row historical table to match.

---

## Routing documentation

Current clone, ownership, and agent-routing overviews live in `README.md`,
`README.en.md`, `AGENTS.md`, and `.agents/README.md`. Those files carry the
current organization owner. The earlier sibling-agent note that this branch
would not edit them is historical and does not apply to the integrated tree.

Coordinator task/provider/worker ownership, coordinator → remote-dev
transport direction, scaffold host-allocation authority, and vaws-top
observation-only status are unchanged. Source integration is not
installed-client, runtime, or hardware evidence.
