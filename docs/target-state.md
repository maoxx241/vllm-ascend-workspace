# Target state

Status: current

This is the single approved contract for the four external components and
this consumer workspace. It supersedes earlier split notes that treated the
workspace as a fifth runtime layer, injected a VAWS resolver into remote-dev,
or kept request/recovery/interpreter/queue logic in `.agents/`.

A mechanism this document does not name is a signal to amend this document
first, not to build the mechanism here.

## 1. Axioms

1. **Four components, one consumer workspace.** remote-dev, vaws-coordinator,
   vaws-knowledge, and vaws-top are the runtime owners. This repository is
   the vLLM-Ascend project that installs and uses them. It is not a fifth
   scheduler, allocator, or execution state machine. "Scaffold" is only the
   historical name of this tree.
2. **Everything packageable is packaged.** The four components are Python
   packages or uvx apps pinned by `uv.lock`. This workspace is a
   `package = false` uv project: project materials, client wiring, and
   business skills. `repo-init` runs `uv sync`.
3. **The package version is the contract.** No `service-api.json`, no
   consumer handshake file. A breaking change is a version bump.
4. **One concern, one owner.** A second implementation, vendored copy, or
   "thin layer" that re-implements package behaviour is a defect. A pure
   re-export that adds no behaviour is not a second implementation.
5. **Unreleased, so breaking is allowed.** Delete superseded code, flags,
   and tests. Do not deprecate.
6. **Local only.** MCP servers run on the user's machine. Knowledge reaches
   the shared corpus only through pull requests.
7. **Code identity is Git.** Knowledge `content_hash` is the only sanctioned
   non-Git hash, owned by `vaws_knowledge.canonical`.
8. **Simplest mechanism that satisfies the axioms.** Fewer files win.

## 2. Ownership

| Concern | Owner | This workspace keeps | This workspace must not keep |
|---|---|---|---|
| Explicit remote read/edit/bash/search/patch/job/artifact/monitor; generic process control | **remote-dev** | client MCP wiring; skills may pass ordinary `host`/`port`/`user`/`cwd` | VAWS resolver plugin; global Ascend env injection; skill-built SSH; a second job supervisor |
| Persistent user container, environment recipes, task isolation, NPU/port leases, queued execution, recovery | **vaws-coordinator** | native `context_file` passthrough; project environment recipes as data; thin CLI that calls the package | request-id ledger; interpreter discovery; pool tick; session container allocator; machine bootstrap engine; `SessionResourceClient` wrappers |
| Knowledge contract, query, capture, redaction rules | **vaws-knowledge** | `.agents/knowledge/*.v2.yaml`; thin CLIs; curate workflow | a workspace knowledge engine |
| Fleet observation | **vaws-top** | `npu-fleet-monitor` uvx skill | a second dashboard |
| Result Envelope v1 (business operation report) | **this workspace** | `vaws_result_envelope.py` and conversion from remote-dev results | an execution state model; per-skill progress sentinels |
| Client configuration | **this workspace** | `vaws_client_setup.py` writing ordinary package MCP/hook entries | injecting consumer resolvers or coordinator internals into remote-dev |
| vLLM-Ascend business skills | **this workspace** | model/preset/TP/PP/PD params, health/first-token, workloads, metrics, reports | allocate→start→release chains; recovery of admission; guessed task identity |
| Project materials | **this workspace** | `vllm/` / `vllm-ascend/` submodules, pins, recipe files, project knowledge data | executing those recipes (coordinator) |

Dependency direction:

```
workspace ──► vaws-coordinator ──► vaws-remote-dev
workspace ──► vaws-knowledge
workspace ──uvx──► vaws-top
```

No package imports this workspace. Coordinator may use remote-dev one-way.
remote-dev does not know VAWS sessions, bindings, profiles, or leases.

## 3. The four components

### 3.1 remote-dev

Generic operations across local platforms and clients. Input is an ordinary
endpoint (`host`, `port`, `user`, `root`, `cwd`) plus operation parameters.
It does not parse VAWS identity, create tasks, or allocate NPUs. Generic
process `prepare` / `go` / `status` / `tail` / `stop` lives here.

Users who already know IP and port use remote-dev directly.

### 3.2 vaws-coordinator

Persistent local manager for this user's environments, tasks, and resources.
One long-lived `vaws-<user>` container per physical host (example:
`vaws-maoxx241`). Business code, builds, services, and tests run inside that
container. The host is infrastructure only.

Public task actions, reused not replaced:

| Action | Agent supplies | Package does |
|---|---|---|
| `session` | native context; optional worktree binds | associate the real native task; do not guess from cwd/history |
| `run` | command plus `env` / `environment` / `resources` / `topology` / `timeout_seconds` / `service` / `restart` | choose hosts, prepare/reuse the user container and task root, allocate devices/ports, launch, wait, finish. No required skill `request_id` / `profile_key` / `runtime_id` / Python path |
| `execution` | business execution reference | refresh facts, tail, stop, return the ordinary endpoint |
| `finish` | close this task | stop owned executions; keep container, roots, evidence |

Services use a **task-scoped business name**. The package owns execution
association and reconnect. Skills may write business reports; those reports
are not recovery authority.

Queued / waiting / starting / uncertain are not running and may have no
service port. The package advances them. Skills report those facts and may
wait for business health once a live target+port exists.

### 3.3 vaws-knowledge

Queryable facts and sourced reference knowledge. A degraded answer is never
an authoritative "no". Knowledge does not schedule work.

### 3.4 vaws-top

Unchanged observation. It does not allocate.

## 4. This workspace

Three kinds of content only:

1. **Project materials** — vLLM / vLLM-Ascend checkouts, dependency pins,
   default config, environment recipes as data, project knowledge YAML.
2. **Install and client wiring** — `uv sync`, `vaws_client_setup.py`,
   passing the native `context_file` / `VAWS_CONTEXT_FILE` into the
   coordinator client. Tool protocol adapters belong to their packages.
3. **Business skills** — model arguments, parallelism, PD roles, readiness
   and first-token checks, benchmark workloads, profiling analysis, reports.

`.vaws-local/` is a storage location. Putting coordinator or remote-dev
state under it does not make this workspace the owner.

### 4.1 Libraries that remain

| File | Why it is workspace |
|---|---|
| `vaws_result_envelope.py` | business operation report |
| `vaws_leak_guard.py` / `vaws_redaction.py` | tracked-tree walk over package rules |
| `vaws_comparability.py` | paired-measurement certificate |
| `vaws_local_state.py` | untracked layout, workspace identity, machine username document |
| `vaws_capability.py` / `vaws_dependency.py` | install/capability reporting |
| `vaws_coordinator_launch.py` | env for the installed coordinator process |
| `vaws_remote_dev.py` | thin import of the installed transport; no resolver, no global Ascend profile |
| `vaws_validate.py` / `vaws_venv.py` | id/env checks; local interpreter hop |
| `vaws_knowledge_service.py` | project/candidate I/O over the package |

Deleted from the destination: consumer remote-dev resolver, session.json
lease authority, `SessionResourceClient` wrappers, machine bootstrap engine,
request/recovery ledgers, interpreter selection, host-queue shipping
shims used as allocators.

### 4.2 Skills

A skill script may call `vaws_coordinator.task_client.TaskClient`,
`remote_dev` explicit-endpoint tools, and `vaws_knowledge` query/capture.
It may not allocate leases, invent request ids, pick Python/CANN, tick the
pool, or infer task identity from cwd or chat history.

Managed NPU work is **one** coordinator `run` call. Status reads facts and
may wait for business health. Stop/finish is coordinator `execution` /
`finish`. A benchmark against a live service uses that service's
authoritative reference and must not acquire the same NPUs again.

## 5. Cross-repository contracts

### 5.1 Version is the contract

`importlib.metadata.version(...)` plus `uv.lock`. `vaws_deps.py doctor`
warns on pin drift and continues.

### 5.2 Run Manifest v1

Exactly `vaws_coordinator.run_manifest`. Git identity in `code`.

### 5.3 Two result contracts

`remote-dev.result.v1` is one remote tool call. Result Envelope v1 is one
agent-facing **business** operation. Skills convert; they do not invent a
third execution state machine. The conversion is named and tested.

### 5.4 Knowledge

Schema v2 as shipped by `vaws-knowledge`. Project layer
`.agents/knowledge/*.v2.yaml`. Candidate layer under `.vaws-local/`.

### 5.5 Endpoints

Ordinary `host` / `port` / `user` / `root` / `cwd`. Coordinator returns
these for a bound execution. remote-dev never learns VAWS object names.

## 6. Deletion rules

- Delete, do not deprecate.
- Dated `docs/` with `Status: dated` stay dated evidence.
- Current-contract documents that contradict this file are rewritten or
  deleted, not kept as compatibility.
- Preserve real user checkouts and live containers. Source cleanup is not
  authorization to rename or remove `vaws-<user>` containers.

## 7. Out of scope here

- `ascend-profiling-analysis` analyser algorithms (keep; only how it reaches
  a host changes).
- Knowledge content extension.
- Publishing this workspace as a package.
- Any hosted multi-user service.
- Mutating live user containers in this implementation pass.

## 8. Decisions

| Date | Decision |
|---|---|
| 2026-09-08 | Pin drift warns, never blocks |
| 2026-09-08 | remote-dev is an installed tool dependency |
| 2026-09-09 | Package version is the contract |
| 2026-09-10 | Four components only; workspace is the consumer project |
| 2026-09-10 | One `vaws-<user>` container per host; coordinator owns bootstrap and execution |
| 2026-09-10 | remote-dev stays independently usable with explicit host/port |
| 2026-09-10 | Skills do not store a recovery ledger; coordinator owns run association |
