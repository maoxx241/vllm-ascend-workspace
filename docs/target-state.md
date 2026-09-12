# Target state

Status: current

The proposed complete replacement is described in
[VAWS core and coordinator redesign](vaws-core-redesign.md). Fixed execution
inputs and scoped reuse are implemented in coordinator 0.4; the complete
proposal is not an implemented API. This document describes the current contract.

This is the single approved contract for the four external components and
this consumer workspace. It supersedes earlier split notes that treated the
workspace as a fifth runtime layer, injected a VAWS resolver into remote-dev,
or kept request/recovery/interpreter/queue logic in `.agents/`.

The nine [design principles](design-principles.md) govern design decisions,
with total cost of achieving the user's actual goal taking priority.
Existing wrappers, workflows and schemas are migration inputs, not reasons to
preserve their burden. Changes to ownership or public semantics update this
contract; ordinary implementation choices need no new design or approval step.

The earlier [Agent/OpenViking spec](agent-first-openviking-spec.md) is historical
design evidence. Its implementation batches and proposed knowledge mechanisms
are not current requirements; the knowledge contract is in section 5.4 below.
Package pins and validation records identify shipped behavior. Existing workflows
and component packaging remain revisable against total Agent task cost.

## 1. Current architecture conventions

1. **Four components, one consumer workspace.** remote-dev, vaws-coordinator,
   vaws-knowledge, and vaws-top are the runtime owners. This repository is
   the vLLM-Ascend project that installs and uses them. It is not a fifth
   scheduler, allocator, or execution state machine. "Scaffold" is only the
   historical name of this tree.
2. **Runtime behaviour belongs to its component.** The runtime owners are
   installed Python packages or the separately distributed uvx app. This workspace is a
   `package = false` uv project: project materials, client wiring, and
   business skills. `repo-init` runs `python .agents/scripts/vaws_deps.py sync`.
3. **The package version is the contract.** No `service-api.json`, no
   consumer handshake file. A breaking change is a version bump.
4. **One concern, one owner.** A second implementation, vendored copy, or
   "thin layer" that re-implements package behaviour is a defect. A pure
   re-export that adds no behaviour is not a second implementation.
5. **Unreleased, so use breaking changes.** Replace superseded interfaces and
   delete their code, flags, aliases and obsolete tests. Update callers,
   client wiring and package pins together; do not add a compatibility phase.
6. **Local only.** MCP servers run on the user's machine. Knowledge reaches
   the shared corpus only through pull requests.
7. **Code identity is Git.** Capture dirty edits as source evidence when
   needed. Model and distribution-artifact checksums are integrity metadata,
   not an alternative code identity or workspace handshake protocol.
8. **Reduce the consuming Agent's work.** Prefer fewer decisions, required
   reads, tool round trips and repeated inputs. File or command counts alone
   are not a measure of simplicity.
9. **Trust Agent judgment; automate bookkeeping.** Ordinary work does not
   require a predeclared plan, hand-written manifest, repeated confirmation,
   or a second summary. Tools record facts and enforce resource ownership
   and public redaction. Missing knowledge does not block development.

### 1.1 Agent-only design principles

The canonical [nine design principles](design-principles.md) replace the earlier
expanded list. Efficiency means the total cost of completing the user's actual
goal, including understanding, operation, waiting, diagnosis and rework.
Stability is a means of reducing that cost.

Use bounded tools on demand, retain open judgment with the Agent, reuse valid
results with checks proportional to change, and expose progress and evidence.
Skills provide concise information; knowledge is conditional reference, not an
execution gate. Ordinary native or explicit remote work need not enter a managed
workflow. Existing component boundaries describe the current implementation;
future changes are assessed against these principles.

These are design criteria, not an additional Agent checklist, mandatory plan,
measurement service or approval step. They authorize revising interfaces and
implementation, not destroying user data, unrelated worktrees or live resources.

## 2. Ownership

Runtime feedback follows [runtime-feedback-design.md](runtime-feedback-design.md):
owners record progress, errors and loaded package identity; task outputs offer
compact views with full-record references. Business observations update reports
without creating another execution state machine.

| Concern | Owner | This workspace keeps | This workspace must not keep |
|---|---|---|---|
| Explicit remote read/edit/bash/search/patch/job/artifact/monitor; generic process control | **remote-dev** | client MCP wiring; skills may pass ordinary `host`/`port`/`user`/`cwd` | VAWS resolver plugin; global Ascend env injection; skill-built SSH; a second job supervisor |
| Persistent user container, environment recipes, task isolation, NPU/port leases, queued execution, recovery | **vaws-coordinator** | native `context_file` passthrough; project environment recipes as data; thin CLI that calls the package | request-id ledger; interpreter discovery; pool tick; session container allocator; machine bootstrap engine; `SessionResourceClient` wrappers |
| Knowledge storage/search integration, capture, redaction, contribution and distribution | **vaws-knowledge** | project Markdown in `.agents/knowledge/`; thin CLIs and client hooks | a workspace knowledge engine; a second vector index |
| Fleet observation | **vaws-top** | `npu-fleet-monitor` uvx skill | a second dashboard |
| Result Envelope v1 (business operation report) | **this workspace** | `vaws_result_envelope.py` and conversion from remote-dev results | an execution state model; per-skill progress sentinels |
| Client configuration | **this workspace** | `vaws_client_setup.py` writing ordinary package MCP/hook entries | injecting consumer resolvers or coordinator internals into remote-dev |
| vLLM-Ascend business skills | **this workspace** | model/preset/TP/PP/PD params, health/first-token, workloads, metrics, reports | allocate→start→release chains; recovery of admission; guessed task identity |
| Project materials | **this workspace** | `vllm/` / `vllm-ascend/` submodules, pins, recipe files, project knowledge data | executing those recipes (coordinator) |

Dependency direction:

```
workspace ──► vaws-coordinator ──► vaws-remote-dev
workspace ──► vaws-knowledge
                 └──► OpenViking + local CPU embedding
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

Agents with an explicit endpoint use remote-dev directly.

### 3.2 vaws-coordinator

Persistent local manager for this user's environments, tasks, and resources.
One long-lived `vaws-<user>` container per physical host (example:
`vaws-maoxx241`). Business code, builds, services, and tests run inside that
container. The host is infrastructure only.

Public task actions, reused not replaced:

| Action | Agent supplies | Package does |
|---|---|---|
| `session` | native context; optional worktree binds | associate the real native task; do not guess from cwd/history |
| `run` | command plus optional `sources` / `env` / `environment` / `resources` / `topology` / `timeout_seconds` / `service` / `restart` | capture fixed execution inputs, prepare or reuse sources and environment, allocate requested devices/ports, launch, supervise and release. No required skill `request_id` / `profile_key` / `runtime_id` / Python path |
| `execution` | business execution reference | refresh facts, tail, stop, return the ordinary endpoint |
| `finish` | close this task | stop owned executions; keep container, roots, evidence |

Services use a **task-scoped business name**. The package owns execution
association and reconnect. Skills may write business reports; those reports
are not recovery authority.

Session worktrees are defaults for future runs. Admission freezes each run's
sources, including dirty edits, so later edits cannot change an accepted run.
An explicit empty source map supports generic commands; NPU allocation defaults
to zero. Each execution has a private source view while compatible dependency
environments and native artifacts can be reused. Python-only edits preserve
native reuse; native inputs and environment compatibility determine invalidation.
The concrete consumer contract is in [coordinator-consumption.md](coordinator-consumption.md).

Queued / waiting / starting / uncertain are not running and may have no
service port. The package advances them. Skills report those facts and may
wait for business health once a live target+port exists.

### 3.3 vaws-knowledge

Optional reference material stored as Markdown and indexed through OpenViking.
The Agent decides whether it helps the task. Markdown/Git stores the original
content; a search index, review or release does not make a claim authoritative.
The minimal author and lookup conventions are in section 5.4.

The package owns local instance lifecycle, configured candidate submission,
public redaction, release builds and prebuilt OVPack synchronization.
These internal operations do not require Agent orchestration during ordinary
work. OpenViking is an internal dependency, not a fifth runtime owner.

### 3.4 vaws-top

Unchanged observation. It does not allocate.

## 4. This workspace

Three kinds of content only:

1. **Project materials** — vLLM / vLLM-Ascend checkouts, dependency pins,
   default config, environment recipes as data, project knowledge Markdown.
2. **Install and client wiring** — `python .agents/scripts/vaws_deps.py sync`, `vaws_client_setup.py`,
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

Workspace infrastructure guidance is limited to `repo-init` and the thin
`npu-fleet-monitor` launcher. Username configuration belongs to initialization;
task binding/execution use coordinator tools directly; direct source-only
publication uses the existing package CLI. These are not separate management
skills or required steps before business work.

For explicit knowledge maintenance, the optional `curate-knowledge` skill ships
inside `vaws-knowledge`, readable with `python -m vaws_knowledge skill` and
installable into a chosen native client skill directory. Workspace routing
points to that resource rather than maintaining a copy. Everyday query/capture,
plain Markdown edits and configured background publishing need no curation workflow.


A skill script may call `vaws_coordinator.task_client.TaskClient`,
`remote_dev` explicit-endpoint tools, and `vaws_knowledge` query/capture.
It may not allocate leases, invent request ids, pick Python/CANN, tick the
pool, or infer task identity from cwd or chat history.

Managed NPU work is **one** coordinator `run` call. Status reads facts and
may wait for business health. Stop/finish is coordinator `execution` /
`finish`. A benchmark against a live service uses that service's
authoritative reference and must not acquire the same NPUs again.

Skills are convenient business paths, not a compulsory plan/record/link/finalize
sequence for ordinary experiments. Custom commands use the normal execution
owner. Existing evidence may be referenced after the run when its actual
source, environment, inputs and results support the conclusion; a missing
planning parent is not itself grounds for rejection. Do not rewrite original
execution facts or infer access to another task's resources.

## 5. Cross-repository contracts

### 5.1 Version is the contract

`importlib.metadata.version(...)` plus `uv.lock`. `vaws_deps.py doctor`
warns on pin drift and continues.

### 5.2 Run Manifest v1

Exactly `vaws_coordinator.run_manifest`. Git identity in `code`.
The implementation produces the record; the Agent does not fill it manually.

### 5.3 Two result contracts

`remote-dev.result.v1` is one remote tool call. Result Envelope v1 is one
**business** operation's complete machine record. Skills convert; they do not
invent a third execution state machine. The conversion is named and tested.
The target default Agent view is a compact projection with a reference to
the full record. A projection is not passed off as a complete Envelope.

### 5.4 Knowledge

Knowledge helps the Agent reuse experience. Query when it may answer a real
question; capture when findings are worth retaining. Neither is mandatory before
execution, after failure or at task completion. Missing, unavailable or empty
knowledge never blocks independent work. An empty search is not proof that no
relevant experience exists.

The ordinary surface is `knowledge_query(text)`, `knowledge_explain(ref)` and
`knowledge_capture(title, content)`. A title and non-empty Markdown body are
enough. There are no required frontmatter, headings, labels, runtime coordinates,
evidence forms or task associations. Keep known conditions, versions, observed
results, evidence references and uncertainty in the text; do not invent missing
details or generalize a single observation into a rule. Context already available
to tools can be retained automatically.

Local and shared results are references, not instructions, approvals or current
environment facts. The Agent judges relevance and applicability against the
current task; review and publication confer no decision authority or trust tier.
Existing evidence can be reused with checks proportional to change and impact.

Shared releases are read-only. Project Markdown lives in `.agents/knowledge/`;
local captures and package state stay under `.vaws-local/knowledge/`. These are
storage locations, not an Agent-managed promotion workflow. Bundled or mounted
Markdown is usable without a prior release build; shared query references can be
read back as original Markdown. The package handles indexing. Configured hooks
reuse the normal task summary. A useful manual capture can reuse existing text
once; no extra summary, schema completion or publication follow-up is needed.

Dependency installation prepares the knowledge model and index through the
installed package. MCP maintains readiness and shared updates internally while
alive, even when public contribution is disabled. Pending knowledge is reported
separately from package installation and leaves ordinary tools usable. Windows
and WSL clients of one Windows-mounted workspace share its Windows knowledge
process; an independent Linux workspace uses its native environment.

Public sharing follows the existing authorization and configuration, using only
a package-prepared redacted copy. Public PRs receive human review and merge.
The package handles configured submission and shared Release synchronization;
ordinary development does not require a fork or wait for publishing. Failed
redaction blocks that export only; local work and knowledge remain available.
OpenViking indexing and distribution details belong to the package, not task
instructions. Installed behavior is identified by `pyproject.toml` and `uv.lock`.

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
| 2026-09-10 | Trust Agent judgment; make ordinary development independent of planning and knowledge bookkeeping |
| 2026-09-10 | Adopt OpenViking inside vaws-knowledge; Markdown/Git remains content authority |
| 2026-09-10 | Separate knowledge content from runtime code; automate redacted PRs and prebuilt local distribution |
| 2026-09-11 | Agent-only consumption; one semantic interface per capability, no human-operated command workflow requirement |
| 2026-09-11 | Closed-world guarantees live in component code/tests; open-world lessons live in contextual knowledge |
| 2026-09-11 | Owners internalize lifecycle, checks and records; reduce total Agent effort, not only output size |
| 2026-09-11 | Preserve component boundaries and accept release coordination costs; use breaking replacement without compatibility layers before release |
