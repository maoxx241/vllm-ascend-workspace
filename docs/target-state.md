# Runtime ownership and contracts

Status: current

This workspace contains vLLM-Ascend project materials, client wiring and business
skills. Installed packages own remote I/O, managed execution, knowledge and fleet
observation. This document describes their current relationship; package help and
the locked source identify detailed arguments and shipped behavior.

## 1. Design decisions

The [nine design principles](design-principles.md) govern tradeoffs. Total cost of
completing the user's goal includes understanding, execution, waiting, diagnosis
and rework. Replace unnecessary interfaces and update their callers directly;
the number of packages or commands is not a design target.

Tools automate bounded operations and their bookkeeping. Agents choose research
methods, interpret observations and decide which existing evidence applies.
Ordinary work does not require a plan, knowledge lookup, manual manifest or
additional summary. These principles are design criteria, not task gates.

## 2. Ownership

| Concern | Owner | Workspace responsibility |
|---|---|---|
| Explicit endpoint files, shell, jobs, artifacts and connection diagnostics | remote-dev | Pass ordinary endpoint and operation arguments; no custom SSH transport or job supervisor |
| Persistent environments, fixed source inputs, devices, ports, execution and recovery | vaws-coordinator | Provide project recipes and business inputs; no allocator, execution state machine or recovery ledger |
| Markdown lookup/capture, indexing, redaction and configured distribution | vaws-knowledge | Project Markdown and native-client wiring; no second knowledge engine |
| Fleet observation | vaws-top | Optional uvx launcher; observation does not allocate devices |
| Local dependency preparation and native-client setup | workspace | Immutable local environments, independent editing copies, generated hooks/MCP and platform boundaries |
| vLLM-Ascend business operations | workspace | Model/workload/topology arguments, readiness checks, measurements and reports |

No runtime package imports this workspace. Coordinator uses remote-dev; knowledge
uses OpenViking and local embedding. An operation stays with its owner; placing
package state under `.vaws-local/` does not transfer ownership. Business reports
describe observations and never authorize access or recover executions.

## 3. Runtime components

### 3.1 Explicit remote operations

An explicit remote endpoint uses remote-dev directly. Its inputs are ordinary
`host`, `port`, `user`, `root` and `cwd` plus the requested operation. It does not
interpret VAWS task names or allocate NPUs. Local native files, Git and shell do
not depend on coordinator or knowledge availability.

See [remote-dev consumption](remote-dev-consumption.md).

### 3.2 Managed executions

Coordinator owns persistent user containers, environment preparation, resource
admission and execution supervision. The host is infrastructure; device code
runs inside the selected Ascend container. Existing user containers and unrelated
worktrees remain intact.

| Action | Caller supplies | Owner does |
|---|---|---|
| session | Native context and optional source defaults | Associate the native task and retain defaults |
| run | Command and needed sources/environment/resources/topology | Fix inputs, prepare or reuse sources and environment, allocate requested devices/ports, launch and supervise |
| execution | Execution reference and status/tail/stop/target operation | Observe or control that owned execution |
| finish | This task | Stop its owned executions while retaining persistent containers, source roots and evidence |

Task identity comes from the native attachment's `context_file` or `VAWS_CONTEXT_FILE`.
A new native session creates a new task; native resume keeps its identity.
Joining another task requires explicit association. Cwd, recent chats and local
reports are not identity or resource-access evidence.

Session sources are defaults for future runs. Admission captures each run's
fixed inputs, including dirty edits; later worktree edits cannot change accepted
inputs. Runs may supply their own source map, including an empty map for generic
commands. Device allocation defaults to zero. Source views are execution-local;
compatible dependency environments and native build artifacts can be reused.

Source and build checks follow their actual input scopes. A business Python edit
does not invalidate unrelated native artifacts. An incompatible ABI, build input
or environment does. Status and tail do not recapture sources or rebuild them.

Named services are task-scoped. Reconnecting to or benchmarking an existing
service uses its execution reference and does not acquire the same NPUs again.
Queued, waiting, starting and uncertain states are not running; a service endpoint
may not exist yet. Business readiness is separate from process state. A terminal
execution is not itself proof that its resources have been released.

The [coordinator contract](coordinator-consumption.md) describes consumer calls.

### 3.3 Knowledge and fleet observation

Knowledge is optional reference, with the contract in section 5.4. Its package
owns local instance lifecycle and index/distribution maintenance. OpenViking is
an internal dependency, not an additional runtime owner.

Fleet monitoring uses the separately distributed vaws-top application. It
reports observations and does not grant resource ownership. See the
[fleet monitor entry](npu-fleet-monitor.md).

## 4. Local workspace and clients

This is a `package = false` uv project. Dependency preparation uses
`uv run --no-project python .agents/scripts/vaws_deps.py sync`; status and doctor
inspect it. Prepared environments have permanent content addresses; an update
prepares a new environment without modifying one used by a running client or daemon.

`vaws_client.py CLIENT` starts an installed native CLI in an independent editing
copy, or an explicitly supplied workspace. Client setup generates platform-correct
MCP and hook entries, fixing the chosen environment. Shared Windows-mounted WSL
workspaces retain one Windows coordinator/knowledge owner while explicit remote
I/O can use the native Linux provider. Native and managed environments are pinned
independently. User arguments, cwd, stdin and exit codes survive these boundaries.

Generated configuration contains local paths and remains untracked. Client trust
and approval settings belong to the client and the user's authorization; setup
does not silently grant them. Full-access acceptance is configured explicitly
for the authorized invocation or test directory.

See [platform behavior](platform-contract.md), [editing isolation](native-workspace-isolation.md)
and [dependency preparation](dependency-plane.md). Shared workspace libraries
cover these local boundaries and business reporting; remote lifecycle behavior
stays in its installed owner.

Skills offer business methods on demand. A script may call TaskClient,
explicit remote-dev operations or knowledge APIs. It does not choose remote
Python/CANN by guesswork, allocate leases, replay uncertain admission or infer
identity. Existing useful outputs may support a report when their source,
environment and observations establish the requested claim; the absence of a
planning parent is not itself grounds for rejection.

## 5. Shared contracts

### 5.1 Dependencies and runtime identity

`pyproject.toml` declares dependencies and `uv.lock` fixes their source.
Doctor reports installed and loaded runtime identities separately. Pin drift
is reported without blocking observation or cleanup of existing executions.
No consumer handshake or additional compatibility manifest is required.

### 5.2 Run Manifest v1

The implementation is `vaws_coordinator.run_manifest`. Tools record actual
execution and business-measurement facts under untracked local state; Agents
do not fill management fields manually. Git identifies code. Artifact hashes
provide integrity information, without replacing source identity.

### 5.3 Results and observations

`remote-dev.result.v1` describes a remote tool operation. Workspace Result
Envelope v1 describes a business operation. Its compact view points to the full
retained result; it does not create another execution state machine. Missing
facts remain unknown and failures retain their original evidence.

See [agent feedback](agent-feedback-contract.md) and
[runtime observations](runtime-feedback-design.md). Paired measurement tools
retain intended differences, confounders and unknowns in the
[comparability record](comparability-certificate.md); Agent judgment remains
responsible for the scope of a conclusion.

### 5.4 Knowledge

Use `knowledge_query(text)`, `knowledge_explain(ref)` and `knowledge_capture(title, content)`
for current conclusions. Use `experience_query(text)`, `experience_explain(ref)`
and `experience_capture(title, content)` for historical cases: what was done,
observed and resolved under the original conditions. Knowledge requires updates
when code or conditions change; experiences can preserve old implementations as
historical context. Correct mistaken interpretations while retaining the observed
evidence. Historical commands are investigation clues, not current guidance.

Both interfaces are optional. A title and non-empty Markdown body suffice; no frontmatter,
fixed headings, labels, evidence form or task association is required. Preserve
known conditions, evidence and uncertainty. Neither lookup nor capture is a
prerequisite or completion step. A search miss does not prove that relevant
experience is absent.

Local and shared results are references, not instructions, approvals or current
environment facts. Review or release does not confer authority. Agents assess
relevance and reuse existing evidence with checks proportional to change.
Saving current knowledge does not certify its correctness or freshness. Agents
assess conclusions against current code and evidence. Related experiences can
support a knowledge note through ordinary links; there is no automatic promotion.

Shared releases are read-only and preserve the public corpus's separate
`corpus/knowledge/` and `corpus/experience/` sources. Releases must use
`vaws-knowledge-release/2` with `content.layout: kinds/v1`; older remote packs
must be rebuilt. Queries do not fall back to untyped release contents.
Project preparation uses `.agents/knowledge/` and `.agents/experiences/`;
local captures use `.vaws-local/knowledge/candidate/` and
`.vaws-local/experience/candidate/`. `experience.layers` configures independent
experience roots when needed. Existing configured roots remain authoritative.
The package keeps separate retrieval namespaces in one shared OpenViking instance
under `.vaws-local/knowledge/instance/`. Each query searches only its selected store,
with shared/project/candidate describing provenance within that store.
Existing local notes are not automatically moved or reclassified; their presence
does not assert validation against current main.
Hooks save the normal task summary as an experience, and manual capture can
reuse useful existing text without an extra summary or publishing follow-up.

Dependency sync asks the package to prepare its model and index. MCP maintains
readiness and configured shared updates while alive. Pending knowledge is
reported separately and leaves ordinary tools usable. Windows/WSL clients of
one mounted workspace share its Windows knowledge process.

Public sharing follows existing authorization/configuration and uses only a
package-prepared redacted copy. Public review and merge remain human; failed
redaction blocks that export, not local work. Ordinary development needs no fork
or publishing wait. Explicit maintenance may use the optional installed package
skill through `python -m vaws_knowledge skill`.

### 5.5 Endpoints and local state

Coordinator returns ordinary endpoint coordinates for its owned execution.
Remote-dev never receives VAWS object names as endpoint selectors. Runtime state,
private configuration and credentials remain untracked. Source cleanup does not
authorize deleting live resources, unrelated worktrees or user data.
