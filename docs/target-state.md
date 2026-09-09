# Target state

Status: current

This document is the single definition of where the workspace ends up after the
repository split. It supersedes the retired `repo-boundaries.md`. A mechanism
this document does not name is a signal to amend this document first, not to
build the mechanism.

This file is a **destination**. It does not describe today's tree. Tree
inventory belongs to a generator or to Git, not to this specification. A
sentence that becomes false because someone landed an unrelated commit does
not belong here.

## 1. Axioms

These were decided by the owner and are not reopened by work packages.

1. **Everything packageable is packaged.** The four external repositories are
   Python packages pinned by `uv.lock`. The scaffold is a `package = false`
   `uv` project: skills, glue, local state, submodules. `repo-init` runs
   `uv sync`; a fresh clone plus `repo-init` is the whole install.
2. **The package version is the contract.** There is no `service-api.json`,
   no `service_api_version` handshake, no side-channel compatibility file.
   A breaking change is a version bump.
3. **Code identity is Git.** A `code` field is a Git SHA (`source_head`,
   `snapshot_commit`). No content hash of source files anywhere. The only
   sanctioned non-Git hash is `content_hash` over a knowledge entry body,
   owned by `vaws_knowledge.canonical`, and a future compile-artifact →
   source mapping if one is ever needed.
4. **Local only.** MCP servers are pulled and run on the user's machine and
   serve that user. Nothing in this project offers a network service to
   others. Knowledge reaches the shared corpus only through pull requests.
5. **One concern, one owner.** For every concern in §2 exactly one repository
   holds the implementation. Everyone else imports it or calls its CLI. A
   second implementation, a vendored copy, or a "thin compatibility layer"
   that re-implements behaviour is a defect, whatever it is called. A
   pure re-export that adds no behaviour is not a second implementation.
6. **Unreleased, so breaking is allowed.** No shim for the previous shape of
   anything in this document. Delete, do not deprecate.
7. **Simplest mechanism that satisfies the axiom above.** When two designs
   both satisfy 1–6, the one with fewer files wins.
8. **Fable designs and accepts; Grok implements.** Acceptance is re-running
   the relevant tests or guards, not reading the diff.

## 2. Ownership matrix

| Concern | Owner | Scaffold keeps | Scaffold must not keep |
|---|---|---|---|
| SSH transport; remote read/write/edit/glob/grep/bash/patch/job/artifact/monitor | **remote-dev** | `vaws_remote_dev.py` (env injection), `vaws_remote_dev_plugin.py` (endpoint resolver), client config | A scaffold SSH transport; skill-side `subprocess` of `ssh`; `vaws_remote_toolbox.py`; `vaws_ssh.py`; skill `remote-toolbox` |
| Task / session identity, runtime pool, NPU and port leases, host queue, machine directory | **vaws-coordinator** | `vaws_coordinator_launch.py`, the *binding* of a worktree to a coordinator session (`current-session.json`); `.agents/lib/vaws_host_queue_module.py` as a pure re-export of `vaws_coordinator.host_queue` | Lease allocation in the scaffold (`allocate_session_leases` … `release_all_session_leases`, `leases.json`); a local `machine-inventory.json` used as an authority |
| Code parity (working tree → remote snapshot) | **vaws-coordinator** | skill `remote-code-parity` as a thin CLI | A scaffold `remote_code_parity.py` body; `vaws_build_inputs.py`; coordinator reading `VAWS_PARITY_SCRIPT` / `VAWS_MACHINE_INVENTORY` |
| Run Manifest v1 and code identity | **vaws-coordinator** (`vaws_coordinator.run_manifest`, `vaws_coordinator.code_identity`) | `run_manifest.py` CLI as a thin wrapper, or nothing if the package ships a CLI | `.agents/lib/vaws_run_manifest.py`, `.agents/lib/vaws_code_identity.py`, a scaffold `run-manifest-v1` schema; coordinator `vendor/` of those modules |
| Knowledge contract, canonical form, `content_hash`, validation, redaction rules, three-layer query, capture, export | **vaws-knowledge** | `.agents/knowledge/*.v2.yaml` (project layer data), thin CLIs `knowledge_query.py` / `knowledge_capture.py` / `knowledge_export.py` / `knowledge_validate.py`, hooks, skill `curate-workspace-knowledge` as workflow only | A scaffold knowledge engine (`vaws_knowledge_v2.py` and its v1/migrate/client siblings); v1 YAML after migration; knowledge JSON schemas with no runtime readers |
| Leak detection rules | **vaws-knowledge** (`vaws_knowledge.redact.RULES`) | `vaws_leak_guard.py` tree walk, allowlist, pre-commit hook; `vaws_redaction.py` BLOCK/EXPORT classification | A second `scan_text` rule set |
| Fleet observation | **vaws-top** | skill `npu-fleet-monitor` (`uvx` release wheel, pidfile, health probe) | A second fleet dashboard or an in-tree vaws-top checkout |
| npu-smi parsing | coordinator for occupancy (`host_queue.parse_npu_smi_info`); vaws-top for display | nothing | Any npu-smi parser in scaffold Python |
| Agent-facing workflow report (Result Envelope v1) | **scaffold** (`vaws_result_envelope.py`, `envelope_lint.py`, `schemas/result-envelope-v1.schema.json`) | all of it | Per-skill progress sentinels and hand-rolled JSON shapes; a contract with no producers |
| Client configuration (hooks, MCP entries for the clients `vaws_client_setup.py` supports) | **scaffold** (`vaws_client_setup.py`) | all | nothing |
| Skills, workflows, decision gates | **scaffold** | `.agents/skills/` | Dead subsystem `.agents/maturation/` |
| Local state layout (`.vaws-local/`) | **scaffold** (`vaws_local_state.py`) | all | State written by deleted concerns (`sessions/leases.json` as a scaffold authority, leftover knowledge candidate JSON, `maturation/`) |
| Guards and CI | **scaffold** | `repo_boundary_check`, `tracked_path_check`, `cli_surface_inventory`, `skill_catalog`, `tracked_leak_scan`, `sync_claude_skills` | Policy that refers to pre-split shapes as if they were current |

Dependency direction after this document is realised:

```
scaffold ──► vaws-coordinator ──► vaws-remote-dev
scaffold ──► vaws-knowledge
scaffold ──uvx──► vaws-top
```

No package imports the scaffold. No package reaches into the scaffold by
file path or environment variable. No cycle.

## 3. The five repositories

### 3.1 remote-dev (`vaws-remote-dev`)

Problem: a remote Linux host over SSH behaves like a local worktree; every
local editor tool has a remote twin plus endpoint fields. Holds no consumer
state, knows no consumer.

### 3.2 vaws-coordinator

Problem: the local process that coordinates *this user's* remote Ascend
containers and host NPU allocation. Not a hosted service.

Grows by exactly the concerns in §2 that move in: parity, run manifest, code
identity, machine directory. Shrinks by `vendor/`. Its reverse dependency on
the scaffold (`VAWS_PARITY_SCRIPT`, `VAWS_MACHINE_INVENTORY`) ends; a
consumer passes data, never a path into its own tree.

### 3.3 vaws-knowledge

Problem: federated, evidence-gated knowledge commons; a failure diagnosed once
on Ascend is not diagnosed twice. Authority for contract, canonical form,
redaction, query, and capture.

### 3.4 vaws-top

Problem: local single-user Ascend fleet monitor. Observes, never allocates.

### 3.5 scaffold (`vllm-ascend-workspace`)

Problem: the place an agent opens. Submodules `vllm/` and `vllm-ascend/`,
the skills that turn package primitives into Ascend workflows, the local
state directory, client configuration, and the guards that keep all of this
honest. Nothing here talks to a remote host except through remote-dev, and
nothing here decides who owns an NPU except through the coordinator.

## 4. Scaffold target shape

### 4.1 `.agents/lib/` after this document

| Stays | Why it is scaffold |
|---|---|
| `vaws_result_envelope.py` | agent-facing workflow contract, owned here (§5.3) |
| `vaws_leak_guard.py` | tracked-tree walk and allowlist; detection delegated |
| `vaws_comparability.py` | domain: paired-measurement certificate |
| `vaws_local_state.py` | `.vaws-local/` layout |
| `vaws_capability.py` | capability / degradation report |
| `vaws_dependency.py` | `pyproject` / `uv.lock` / installed reconciliation |
| `vaws_session_state.py` | binding only: which coordinator session this worktree is attached to |
| `vaws_session_id.py` | `current-session.json` |
| `vaws_redaction.py` | BLOCK/EXPORT classification over commons rules |
| `vaws_knowledge_service.py` | project/candidate configuration and workflow I/O; knowledge rules/query delegated |
| `vaws_coordinator_launch.py` | start the installed coordinator |
| `vaws_remote_dev_plugin.py` | endpoint resolver for the scaffold's machines |
| `vaws_remote_dev.py` | environment injection into remote-dev |
| `vaws_remote_adapters.py` | agent workflow composition |
| `vaws_remote_target.py` | selector-to-endpoint/environment binding; machine authority delegated |
| `vaws_validate.py` | id / env / device CSV validation |
| `vaws_venv.py` | `.venv` re-exec shim |
| `vaws_host_queue_module.py` | pure re-export of `vaws_coordinator.host_queue` so remote host-side loaders still receive a real `Path` |

### 4.2 `.agents/scripts/`

Guards (`repo_boundary_check`, `tracked_path_check`, `cli_surface_inventory`,
`skill_catalog`, `tracked_leak_scan`, `sync_claude_skills`, `envelope_lint`),
dependency plane (`vaws_deps`, `vaws_client_setup`, `vaws`), identity
(`workspace_identity`, `workspace_profile`), and the thin knowledge CLIs.
Everything that was a wrapper over a deleted library goes with the library.

### 4.3 Skills

A skill is a `SKILL.md`, a workflow, decision gates, and scripts that compose
package primitives. A skill script may call `remote_dev.core.*`,
`vaws_coordinator.*`, `vaws_knowledge.*`, and other skills' published entry
points. A skill script may not open an SSH connection, parse `npu-smi`,
allocate a lease, or hash a knowledge body itself.

The envelope this document keeps as scaffold-owned (§5.3) is a contract only
if skills emit it. Per-skill progress sentinels collapse to the one sentinel
the envelope library defines. The load-bearing producers are
`vllm-ascend-serving`, `remote-code-parity`, `session-management`, and
`machine-management`.

SSH strategy legitimately forks. Short commands multiplex; long streams must
not. That divergence is a recorded failure signature, so unmultiplexed
access is an endpoint option rather than an accident to flatten.

`ascend-profiling-analysis` analysis logic is out of scope for this round by
owner decision; only its `knowledge/` directory moves (§7). Its analysis
framework runs *inside the remote container* after a push of its own
`ascend_profile` package tree. When that skill is in scope, that push
becomes a remote-dev artifact push. How the skill reaches a host is a
remote-dev concern now; the analyser itself is not.

Every skill has a discoverable `tests/` directory, and every one of them is
in CI.

### 4.4 Entry points

Tracked Python under `.agents/` divides into two populations, and they get
opposite treatment. The CLI-surface inventory is the authority for which
files are entry points. Test files (`tests/` directories, `test_*.py`,
`selftest_*.py`, `*_test.py`, `conftest.py`) are not entry points: pytest
does not go through `__main__`, and they do not receive the shim. A
`__main__` file that is neither a test nor in the inventory is a
classification gap the guard flags, not a file to shim by default.

**Local entry points** — inventoried files the inventory does **not**
classify as `payload` — carry `ensure_workspace_interpreter`, so that
`python3 path/to/script.py` works from a shell that never activated
`.venv`.

**Remote payloads** must not carry it. These are inventoried files whose
`__main__` runs on the NPU container, reached by copying the source over
and invoking the container's interpreter. There is no `.venv` and no
`.agents/lib` at the far end, so the shim would add an import that cannot
resolve. Their contract is the opposite one: they import nothing from
`.agents/lib`. Generated Trae byte-copies of those payloads inherit the
same `payload` classification so the shim stays out of both sides of a
byte-identical pair.

The line between the two populations is not a new list to maintain. The
inventory's `payload` class is the input to the guard. A file that
changes population has to change its inventory row in the same commit.
Overlay `payload` means "runs without the workspace interpreter", not
merely "spawned by another command": a workstation helper that another
script launches (for example `remote_code_parity.py`) is `supported`,
not `payload`.

### 4.5 Local state

`.vaws-local/` keeps: `machine-profile.json`, `workspace-identity.json`,
`current-session.json`, `agent-sessions/`, `remote-dev-state/`,
`knowledge/candidate/*.yaml` (commons candidate layer), per-skill run
directories, `client-setup-backups/`. It loses every path in §2's "must
not keep" column. Coordinator and remote-dev state lives where those
packages put it (`VAWS_COORDINATOR_STATE_DIR`, `REMOTE_DEV_STATE_DIR`),
both pointed inside `.vaws-local/` by the scaffold.

## 5. Cross-repository contracts

### 5.1 Version is the contract

A consumer checks `importlib.metadata.version(...)` and the `uv.lock` pin.
`vaws_deps.py doctor` reports drift as a warning and continues (owner
decision 2026-09-08: warn, do not block).

### 5.2 Run Manifest v1

Exactly one module: `vaws_coordinator.run_manifest`. Its `code` field is
required and is Git identity (`source_head`, `snapshot_commit`, both matching
`GIT_SHA_RE`). The scaffold file `.agents/lib/vaws_run_manifest.py` is
absent. The coordinator's `vendor/` copy of that module is absent.

This is not hygiene. Two producers under one version number were writing
incompatible records (one required `code`, one used SHA-256 identity), and
each repository's CI tested only its own copy.

### 5.3 Two result contracts, two levels

`remote-dev.result.v1` (remote-dev, seven required fields) is the result of
**one remote tool call**: `tool`, `target`, `outcome`, `status`, `summary`,
plus `preview` / `refs` / `artifacts` / `changed_files`.

Result Envelope v1 (scaffold) is the report of **one agent-facing
operation**: `envelope_id`, `operation`, `attempt`, `failure` with layer
attribution, `environment`, `evidence`, `next_step`, `parts` for fan-out,
`children` digests, `extensions`.

The coordinator emits remote-dev results because it performs remote tool
calls; skills emit envelopes because they perform operations. The earlier
note that the scaffold should "stop maintaining its own envelope" was wrong
and is withdrawn: it would delete the layer that attributes a failure.

Two levels do **not** mean the outer one accepts the inner one as it stands.
Feeding `remote_dev.result.make_result()` output straight into the
validators fails in all twelve combinations of six outcomes by two slots:

| Slot | Missing fields | Outcome rejections |
|------|----------------|--------------------|
| `parts` | `unit` | `failed`, `timeout`, `needs_input`, `cancelled` are not in `PART_OUTCOMES`; `blocked` additionally requires `layer` |
| `children` | `envelope_id`, `depth` | `failed`, `timeout`, `needs_input` are not envelope outcomes |

The two vocabularies overlap without matching. remote-dev has
`needs_input`, `failed` and `timeout`; the envelope has `partial` and, for
parts only, `skipped`. `cancelled` is a legal envelope outcome but not a
legal part outcome.

So the contract is a **named conversion**, owned by whoever holds Result
Envelope v1, and it must state three things:

1. The outcome mapping, in full, in both directions where a direction
   exists. `failed` to `failure` and `timeout` to `failure` are lossy, so
   the original belongs in the part's or child's evidence rather than being
   discarded.
2. Where `unit`, `envelope_id` and `depth` come from. They are properties of
   the *operation's* structure, not of the remote call, so the caller
   supplies them; a result cannot self-describe as a part.
3. Which layer a converted failure is attributed to. A remote-dev failure is
   not automatically the `remote` layer: a non-zero exit code from a command
   the agent composed is an `operation` failure that happened to travel over
   remote-dev.

The conversion is a function with tests over the full outcome cross-product,
not an assertion in prose.

### 5.4 Knowledge entry

Schema v2 as shipped by `vaws-knowledge`. One body, `rule` or `measurement`.
`content_hash` covers `scope` plus that body and is computed only by
`vaws_knowledge.canonical.content_hash`. The scaffold's project layer is
`.agents/knowledge/*.v2.yaml`; the candidate layer is
`.vaws-local/knowledge/candidate/*.yaml`; the shared layer is the corpus
inside the installed wheel. v1 documents do not exist after migration.

### 5.4a Applicability is reachable

`AGENTS.md` states that applicability is a coordinate, and schema v2 carries
twelve scope dimensions. Commons implements the evaluation
(`evaluate_scope`, `Applicability`, `COVERED` / `MISMATCH` / `UNDECIDABLE` /
`ASSUMED_ANY`) and `query()` accepts a `reader_coordinate`.

The MCP tool schema in the locked `vaws-knowledge` package declares
`reader_coordinate` on both query tools and forwards it to `query()`.

Both CLIs expose the same twelve scope dimensions (`--soc` … `--component`),
a `--reader-coordinate` JSON object, and `--run-manifest`. A query that
names a `soc` an entry's scope excludes does not return that entry. The
response's `reader_coordinate` is populated. A caller inside a run does
not retype what the Run Manifest v1 already recorded:
`vaws_coordinator.run_manifest.load_manifest` fills the derivable
dimensions; anything not derivable stays absent and is reported as
`unknown` / unsupplied. Neither CLI invents a version to fill a hole. A
missing or too-old `vaws-knowledge` (before the coordinate flags) fails
with cause and `uv sync` as the remedy; there is no silent unscoped
fallback. Building a second MCP surface is not part of it.

### 5.5 Redaction

Detection rules are `vaws_knowledge.redact.RULES`. The scaffold classifies
each rule id as BLOCK or EXPORT (`vaws_redaction.py`) and walks its tracked
tree (`vaws_leak_guard.py`). No other regex set for hosts, addresses, users,
or paths exists in the scaffold.

### 5.6 Endpoint

A remote endpoint is `host`, `port`, `user`, `root`, `cwd` as defined by
remote-dev. The scaffold's machine inventory becomes a coordinator-owned
machine directory; the scaffold's resolver plugin maps a machine id to an
endpoint by asking the coordinator.

## 6. Deletion rules

Git history is the archive. This section is a rule, not a named inventory.

- **Delete, do not deprecate.** No compatibility shim for a previous shape
  named in this document (axiom 6).
- **A superseded dated document is deleted**, not kept as a museum. The
  `Status:` convention survives only for documents that are evidence and
  have no successor yet. See [README.md](README.md).
- **A pure re-export is not a second implementation.**
  `vaws_host_queue_module.py` stays for that reason.
- **Two names that collide must not be confused.**
  `.agents/skills/remote-toolbox/` is a skill package.
  `.agents/lib/vaws_remote_toolbox.py` is a transport library. They are
  separate deletions. The library goes only after its importers move to
  remote-dev.

## 7. Out of scope for this round

- `ascend-profiling-analysis` **analysis logic**. Its `knowledge/`
  measurement data moves to `vaws-knowledge` as `measurement` entries (all
  `unverified`, per commons decision 24); the analyser reads them through the
  knowledge client with `include-unverified` and a built-in fallback. The
  split is transport versus analysis, not skill versus skill. How the skill
  reaches a host is remote-dev's concern. The remaining analysis logic is
  not read, moved, or reshaped.
- Publishing the scaffold itself as a package.
- Any hosted or multi-user service.
- Compile-artifact → source mapping for operators (axiom 3 reserves the
  slot; nothing needs it yet).

## 8. Decisions recorded here

| Date | Decision | Why |
|---|---|---|
| 2026-09-08 | Pin drift warns, never blocks | owner |
| 2026-09-08 | remote-dev is a pip-installable pure tool dependency | owner |
| 2026-09-08 | Hashes: Git for code, `content_hash` for knowledge bodies, nothing else | owner |
| 2026-09-08 | Everything local; knowledge shared only by PR | owner |
| 2026-09-09 | Contract is the package version; no `service-api.json` | all four packages converged independently; knowledge decision 28 |
| 2026-09-09 | Run Manifest and code identity live in the coordinator | vendored copy proven incompatible under the same version; coordinator is already the execution authority and already on the dependency path; both modules are stdlib-only |
| 2026-09-09 | Parity and machine directory live in the coordinator | ends the package → scaffold path dependency; the coordinator is the only caller that needs them as a library |
| 2026-09-09 | Two result contracts are two levels, not a duplicate | remote-dev result = one tool call; Envelope = one operation. Withdraws the earlier "converge on `remote-dev.result.v1`" note |
| 2026-09-09 | The lift between the two levels is an owned conversion with an explicit outcome mapping | "composes without reshaping" was checked and is false in all twelve outcome × slot combinations; the vocabularies overlap without matching and `unit`/`envelope_id`/`depth` are properties of the operation, not of the call |
| 2026-09-09 | `.agents/maturation/` is deleted | zero production importers, absent from CI, superseding document already published |
| 2026-09-09 | Superseded dated docs are deleted | Git is the archive |
| 2026-09-09 | `ascend-profiling-analysis` deferred at the analysis/transport line | owner deferred the analyser; reaching a host is remote-dev's concern |
| 2026-09-09 | Skills adopt Result Envelope v1 rather than the envelope being deleted | a contract with no producers is not a contract; the attribution/evidence structure is what agents need |
| 2026-09-09 | Unmultiplexed SSH stays an endpoint option | long-stream `mux=False` is a recorded failure signature, not an accident |
| 2026-09-09 | Reader coordinates become reachable from both CLIs and are auto-filled where derivable | the MCP tool already accepts them; the gap is CLI exposure plus population from the Run Manifest and machine profile. No second MCP surface |
| 2026-09-09 | The interpreter shim applies to inventoried non-`payload` entry points only; tests are not entries; unclassified `__main__` files are classification gaps | remote payloads run on the container with no `.venv` and no `.agents/lib` |
| 2026-09-09 | A non-empty attributed tracked-path baseline is the guard's design | `docs/tracked-path-guard.md` |
| 2026-09-09 | `vaws_host_queue_module.py` stays as a pure re-export | `docs/coordinator-consumption.md`; not a second implementation |
