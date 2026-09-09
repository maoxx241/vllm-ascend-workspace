# Target state

Status: current

This document is the single definition of where the workspace ends up after the
repository split. It supersedes the retired `repo-boundaries.md`. Every remaining
work package derives from this document, and a work package that needs a
mechanism this document does not name is a signal to amend this document
first, not to build the mechanism.

Facts below cite line counts from the 2026-09-09 surveys of the scaffold and
the four packages. Numbers are rounded to what matters for sizing, not audit.

## 1. Axioms

These were decided by the owner and are not reopened by work packages.

1. **Everything packageable is packaged.** The four external repositories are
   Python packages pinned by `uv.lock`. The scaffold is a `package = false`
   `uv` project: skills, glue, local state, submodules. `repo-init` runs
   `uv sync`; a fresh clone plus `repo-init` is the whole install.
2. **The package version is the contract.** There is no `service-api.json`,
   no `service_api_version` handshake, no side-channel compatibility file.
   All four packages already deleted theirs in their packaging commits
   (knowledge decision 28 records it). A breaking change is a version bump.
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
   that re-implements behaviour is a defect, whatever it is called.
6. **Unreleased, so breaking is allowed.** No shim for the previous shape of
   anything in this document. Delete, do not deprecate.
7. **Simplest mechanism that satisfies the axiom above.** When two designs
   both satisfy 1–6, the one with fewer files wins.
8. **Fable designs and accepts; Grok implements.** Work packages carry an
   acceptance predicate from §7 and are accepted by re-running it
   independently, not by reading the diff.

## 2. Ownership matrix

| Concern | Owner | Scaffold keeps | Scaffold deletes |
|---|---|---|---|
| SSH transport; remote read/write/edit/glob/grep/bash/patch/job/artifact/monitor | **remote-dev** | `vaws_remote_dev.py` (env injection), `vaws_remote_dev_plugin.py` (endpoint resolver), client config | `vaws_remote_toolbox.py` (2402), `vaws_ssh.py` (85), 17 × `remote_*.py` wrappers (340), `remote_toolbox_stress.py` (239), skill `remote-toolbox` (389), every `subprocess(["ssh", …])` in skill `_common.py` files |
| Task / session identity, runtime pool, NPU and port leases, host queue, machine directory | **vaws-coordinator** | `vaws_coordinator_launch.py`, the *binding* of a worktree to a coordinator session (`current-session.json`) | lease allocation in `vaws_session_state.py` (`allocate_session_leases` … `release_all_session_leases`, `leases.json`), `vaws_host_queue_module.py` (72; callers import `vaws_coordinator.host_queue` directly), local `machine-inventory.json` as an authority |
| Code parity (working tree → remote snapshot) | **vaws-coordinator** | skill `remote-code-parity` as a thin CLI | `remote_code_parity.py` (2485) body, `vaws_build_inputs.py` (69) — move into coordinator; coordinator stops reading `VAWS_PARITY_SCRIPT` / `VAWS_MACHINE_INVENTORY` |
| Run Manifest v1 and code identity | **vaws-coordinator** (`vaws_coordinator.run_manifest`, `vaws_coordinator.code_identity`) | `run_manifest.py` CLI as a thin wrapper, or nothing if the package ships a CLI | `vaws_run_manifest.py` (336), `vaws_code_identity.py` (224), `schemas/run-manifest-v1.schema.json` (143); coordinator deletes `vendor/` |
| Knowledge contract, canonical form, `content_hash`, validation, redaction rules, three-layer query, capture, export | **vaws-knowledge** | `.agents/knowledge/*.v2.yaml` (project layer data), thin CLIs `knowledge_query.py` / `knowledge_capture.py` / `knowledge_export.py` / `knowledge_validate.py`, hooks, skill `curate-workspace-knowledge` as workflow only | `vaws_knowledge_v2.py` (1638), `vaws_knowledge_v1.py` (1020), `vaws_knowledge_migrate.py` (427), `vaws_knowledge_client.py` (591), `knowledge_migrate_v2.py` (241), `knowledge_hash_parity.py` (190), four `schemas/knowledge-*.json` (932, zero runtime readers), six v1 YAML files after their 21 entries are migrated |
| Leak detection rules | **vaws-knowledge** (`vaws_knowledge.redact.RULES`) | `vaws_leak_guard.py` tree walk, allowlist, pre-commit hook; `vaws_redaction.py` BLOCK/EXPORT classification | `vaws_leak_guard.scan_text` own rule set, `maturation/redact.py` |
| Fleet observation | **vaws-top** | skill `npu-fleet-monitor` (`uvx` release wheel, pidfile, health probe) | nothing further; already reduced to 444 lines |
| npu-smi parsing | coordinator for occupancy (`host_queue.parse_npu_smi_info`); vaws-top for display | nothing | every npu-smi parser in `manage_machine.py`, `mem_collect.py`, serving `_common.py`, `serve_probe_npus.py` |
| Agent-facing workflow report (Result Envelope v1) | **scaffold** (`vaws_result_envelope.py`, `envelope_lint.py`, `schemas/result-envelope-v1.schema.json`) | all of it | eleven per-skill progress sentinels and hand-rolled JSON shapes (§4.3) — see §5.3 |
| Client configuration (hooks, MCP entries for six clients) | **scaffold** (`vaws_client_setup.py`) | all | nothing |
| Skills, workflows, decision gates | **scaffold** | `.agents/skills/` | dead subsystem `.agents/maturation/` (3519, zero production importers, not in CI) |
| Local state layout (`.vaws-local/`) | **scaffold** (`vaws_local_state.py`) | all | state written by deleted concerns above (`sessions/leases.json`, `remote-toolbox/`, `knowledge/candidates/*.json`, `maturation/`) |
| Guards and CI | **scaffold** | `repo_boundary_check`, `tracked_path_check`, `cli_surface_inventory`, `skill_catalog`, `tracked_leak_scan`, `sync_claude_skills` | policy content that refers to pre-split shapes (§7 rewrites it) |

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
state, knows no consumer. This is already true at v0.1.0 (255 tests).

Debts that this document accepts as work: `respect_gitignore` is advertised
but not implemented; CI installs editable and never builds a wheel; README
lists a deleted `docs/` directory.

### 3.2 vaws-coordinator

Problem: the local process that coordinates *this user's* remote Ascend
containers and host NPU allocation. Not a hosted service.

Grows by exactly the concerns in §2 that move in: parity, run manifest, code
identity, machine directory. Shrinks by `vendor/`. Its reverse dependency on
the scaffold (`VAWS_PARITY_SCRIPT`, `VAWS_MACHINE_INVENTORY`) ends; a
consumer passes data, never a path into its own tree.

Debts: CI still carries a private-token step for remote-dev (public since
2026-09-08); host state directory is the literal `/tmp/vaws-npu-coordinator/v1/`.

### 3.3 vaws-knowledge

Problem: federated, evidence-gated knowledge commons; a failure diagnosed once
on Ascend is not diagnosed twice. Already the authority for contract,
canonical form, redaction, query, capture (591 tests; wheel ships the corpus;
PR #15 mounts it by default).

Debts: `server/capture.py` re-implements `content_hash` inside the package
itself; `sync/README.md` still names `tools/*.py` paths from before
packaging.

### 3.4 vaws-top

Problem: local single-user Ascend fleet monitor. Observes, never allocates.
Complete for this document's purposes. Debt: console HTTP routes have no
tests; building from Git needs Node 22 (release wheel does not).

### 3.5 scaffold (`vllm-ascend-workspace`)

Problem: the place an agent opens. Submodules `vllm/` and `vllm-ascend/`,
the skills that turn package primitives into Ascend workflows, the local
state directory, client configuration, and the guards that keep all of this
honest. Nothing here talks to a remote host except through remote-dev, and
nothing here decides who owns an NPU except through the coordinator.

## 4. Scaffold target shape

### 4.1 `.agents/lib/` after this document

| Stays | Lines | Why it is scaffold |
|---|---:|---|
| `vaws_result_envelope.py` | 1507 | agent-facing workflow contract, owned here (§5.3) |
| `vaws_leak_guard.py` | 1329 → smaller | tracked-tree walk and allowlist; detection delegated |
| `vaws_comparability.py` | 654 | domain: paired-measurement certificate |
| `vaws_local_state.py` | 514 | `.vaws-local/` layout |
| `vaws_capability.py` | 509 | capability / degradation report |
| `vaws_dependency.py` | 340 | `pyproject` / `uv.lock` / installed reconciliation |
| `vaws_session_state.py` | 880 → binding only | which coordinator session this worktree is attached to |
| `vaws_session_id.py` | 254 | `current-session.json` |
| `vaws_redaction.py` | 179 | BLOCK/EXPORT classification over commons rules |
| `vaws_coordinator_launch.py` | 123 | start the installed coordinator |
| `vaws_remote_dev_plugin.py` | 121 | endpoint resolver for the scaffold's machines |
| `vaws_remote_dev.py` | 105 | environment injection into remote-dev |
| `vaws_validate.py` | 76 | id / env / device CSV validation |
| `vaws_venv.py` | 55 | `.venv` re-exec shim |

Fourteen modules, about 6 500 lines, down from twenty-four and 13 510.

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

Twenty-five skills, 101 407 lines. Four are load-bearing by in-degree —
`vllm-ascend-serving`, `remote-code-parity`, `session-management`,
`machine-management` — and they are exactly the four that hold the duplicated
SSH and npu-smi code. Fixing those four fixes most of §2's "deletes" column,
because the other skills reach remote hosts *through* them.

Two facts constrain how the skill layer is cleaned:

**Every skill invented its own result shape.** No skill imports
`vaws_result_envelope.py`. Instead there are eleven distinct progress
sentinels — `__VAWS_PROGRESS__`, `__VAWS_SERVING_PROGRESS__`,
`__VAWS_PARITY_PROGRESS__`, `__VAWS_BENCHMARK_PROGRESS__`,
`__VAWS_SESSION_PROGRESS__`, `__VAWS_PROFILE_ANALYSIS_PROGRESS__`,
`__VAWS_MATURATION_PROGRESS__`, `__VAWS_REMOTE_TOOLBOX_PROGRESS__`,
`__VAWS_PROFILING_COLLECTION_PROGRESS__`,
`__VAWS_NPU_COORDINATION_PROGRESS__`, `__VAWS_MEMPROF_PROGRESS__` — plus
`__VAWS_JSON__` for terminal payloads, three of them hand-written rather than
delegated to the shared library. Two disappear with the deletions in §2
(`maturation`, `remote_toolbox`); nine belong to skills that stay.

So the envelope this document keeps as scaffold-owned (§5.3) is, today, a
contract with no producers. Either the skills adopt it or it is not a
contract; the work list picks adoption, starting with the four load-bearing
skills.

**SSH strategy legitimately forks.** Short commands multiplex; long streams
must not (`mux=False` in the analysis and collection wrappers). That divergence
is already a recorded failure signature, so the remote-dev migration has to
preserve it as an endpoint option rather than flatten it.

`ascend-profiling-analysis` (44 179 lines, 30 % of the scaffold) is out of
scope for this round by owner decision; only its `knowledge/` directory moves
(§8). One property of it does bear on §2: its analysis framework runs *inside
the remote container* after a tar-over-ssh push of its own
`ascend_profile` package tree, which is a third code-transport mechanism
next to parity and remote-dev
artifacts. When it is in scope, that push becomes a remote-dev artifact push.

Skills with no tests at all: `ascend-profiling-collection` (its two
`selftest_*.py` sit in `scripts/`, outside discovery), `modelscope`,
`remote-toolbox`. Skills absent from CI: those three plus
`repo-init/test_workspace_identity.py` (538 lines, in the skill root rather
than a `tests/` directory). `modelscope` is referenced by no other skill.

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
`.venv`. A guard test enforces this, and CI runs the entry-point smoke
under the system interpreter as well as `uv run`, because the two
disagreeing is exactly the failure CI was not catching.

**Remote payloads** must not carry it. These are inventoried files whose
`__main__` runs on the NPU container, reached by copying the source over
and invoking the container's interpreter: `weight_inspector.py` is read
and written to `/tmp/_vaws_weight_inspector.py`, and the tensor-dump
assets are imported inside the vLLM process. There is no `.venv` and no
`.agents/lib` at the far end, so the shim would add an import that cannot
resolve. Their contract is the opposite one: they import nothing from
`.agents/lib`. Generated Trae byte-copies of those payloads inherit the
same `payload` classification so the shim stays out of both sides of a
byte-identical pair.

The line between the two populations is not a new list to maintain. The
inventory's `payload` class is the input to the guard. A file that
changes population has to change its inventory row in the same commit,
which is the point. Overlay `payload` means "runs without the workspace
interpreter", not merely "spawned by another command": a workstation
helper that another script launches (for example `remote_code_parity.py`)
is `supported`, not `payload`.

### 4.5 Local state

`.vaws-local/` keeps: `machine-profile.json`, `workspace-identity.json`,
`current-session.json`, `agent-sessions/`, `remote-dev-state/`,
`knowledge/candidate/*.yaml` (commons candidate layer), per-skill run
directories, `client-setup-backups/`. It loses every path in §2's "deletes"
column. Coordinator and remote-dev state lives where those packages put it
(`VAWS_COORDINATOR_STATE_DIR`, `REMOTE_DEV_STATE_DIR`), both pointed inside
`.vaws-local/` by the scaffold.

## 5. Cross-repository contracts

### 5.1 Version is the contract

A consumer checks `importlib.metadata.version(...)` and the `uv.lock` pin.
`vaws_deps.py doctor` reports drift as a warning and continues (owner
decision 2026-09-08: warn, do not block).

### 5.2 Run Manifest v1

Exactly one module: `vaws_coordinator.run_manifest`. Its `code` field is
required and is Git identity (`source_head`, `snapshot_commit`, both matching
`GIT_SHA_RE`). The scaffold's current `vaws_run_manifest.py` is the version
that moves; the coordinator's `vendor/vaws_run_manifest.py` (upstream ref
`161fed1`, no `code` field, SHA-256 identity) is deleted.

This is not hygiene. On 2026-09-09 a manifest produced by the vendored copy
was rejected by the scaffold's validator with `missing top-level fields:
code` while both declared `schema_version: 1`. Two producers under one
version number were writing incompatible records, and each repository's CI
tested only its own copy.

### 5.3 Two result contracts, two levels

`remote-dev.result.v1` (remote-dev, 781-byte schema, seven required fields)
is the result of **one remote tool call**: `tool`, `target`, `outcome`,
`status`, `summary`, plus `preview` / `refs` / `artifacts` / `changed_files`.

Result Envelope v1 (scaffold, seventeen required fields) is the report of
**one agent-facing operation**: `envelope_id`, `operation`, `attempt`,
`failure` with layer attribution, `environment`, `evidence`, `next_step`,
`parts` for fan-out, `children` digests, `extensions`.

The coordinator emits remote-dev results because it performs remote tool
calls; skills emit envelopes because they perform operations. The earlier
note that the scaffold should "stop maintaining its own envelope" was wrong
and is withdrawn: it would delete the layer that attributes a failure.

Two levels do **not** mean the outer one accepts the inner one as it stands.
An earlier draft of this section said a result lifts into `parts` or
`children` "without reshaping", citing `vaws_result_envelope.py:192`. That
citation only covers `preview` field compatibility, and the claim is false.
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

The MCP path is already wired: the tool schema in the locked
`vaws-knowledge` package declares `reader_coordinate` on both query tools
and forwards it to `query()`. An agent speaking MCP can ask "does this
apply to my container" today.

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

## 6. Deletion inventory

Before touching any skill script, this document deletes about 19 000 tracked
lines:

| Area | Lines |
|---|---:|
| `.agents/lib/` modules moved or deleted (§4.1) | ≈ 6 900 |
| `.agents/scripts/` wrappers over deleted libraries | ≈ 1 100 |
| `.agents/maturation/` | 3 519 |
| `.agents/schemas/knowledge-*.json`, `run-manifest-v1.schema.json` | 1 075 |
| skill `remote-toolbox` | 389 |

Two of these names collide and must not be confused when the deletion is
executed. `.agents/skills/remote-toolbox/` is five markdown files with no
scripts, referenced only by documents and the leak allowlist; deleting it is
safe on its own. `.agents/lib/vaws_remote_toolbox.py` is the 2402-line
implementation with 31 importers across ten skills, and it can only go after
those importers move to remote-dev. They are separate steps in that order,
and P13 names only the first.
| `docs/audits/` (103 tracked-path violations among them), `deterministic-core-maturation.md`, `leak-remediation.md`, `repo-boundaries.md` | ≈ 5 600 |
| `.agents/policy/tracked-paths-baseline.json` (mostly audit paths) | ≈ 900 |
| `.agents/knowledge/*.yaml` v1 after migration | 752 |

Git history is the archive for dated evidence. A `Status: dated` document
that has been superseded is deleted, not kept; the `Status:` convention
survives only for documents that are evidence and have no successor yet.

## 7. Acceptance predicates

Each predicate is a shell or Python check that a work package's acceptance
runs on a fresh clone after `repo-init`. All must hold at the end; a work
package names the subset it makes true.

| # | Predicate |
|---|---|
| P1 | No scaffold Python spawns `ssh`: `rg -l '"ssh"' .agents --glob '*.py'` returns nothing outside `vaws_remote_dev*.py` configuration. |
| P2 | No `npu-smi` string in any scaffold Python file. |
| P3 | No writer of `leases.json` in the scaffold; `rg allocate_session_leases .agents` is empty. |
| P4 | `rg 'def content_hash\|def canonical_payload\|def canonical_object' .agents` is empty. |
| P5 | `rg 'def scan_text' .agents` is empty; leak guard imports `vaws_knowledge.redact`. |
| P6 | `import vaws_coordinator.run_manifest` succeeds; `.agents/lib/vaws_run_manifest.py` and the coordinator's `vendor/` are absent; a manifest written by `vaws_coordinator.ready_runtime` passes `vaws_coordinator.run_manifest.validate_manifest`. |
| P7 | `rg 'VAWS_PARITY_SCRIPT\|VAWS_MACHINE_INVENTORY'` across the coordinator source is empty. |
| P8 | `.agents/knowledge/` contains only `*.v2.yaml`; `vaws-knowledge validate .agents/knowledge` exits 0. |
| P9 | Every inventoried CLI-surface entry that is **not** classified `payload` contains `ensure_workspace_interpreter`; every inventoried `payload` entry imports nothing from `.agents/lib`. Test files are not entry points and do not receive the shim. A `__main__` file that is neither a test nor in the inventory is a classification gap the guard flags. One guard test asserts those three facts against the inventory. |
| P10 | `tracked_path_check.py --mode enforce` passes with an empty baseline. |
| P11 | `rg -l 'Status: dated' docs` is empty. |
| P11a | A pull request that touches only `docs/` runs the document guards. Until 2026-09-09 the job holding them was filtered to `.agents/**`, so a docs-only change merged without the anti-rot guard whose subject is tracked documents. |
| P12 | Fresh clone → `repo-init` → `python3 .agents/scripts/vaws_deps.py doctor` reports `success` with all capabilities available, using the system `python3`. |
| P13 | `.agents/maturation/` and `.agents/skills/remote-toolbox/` do not exist; `docs/README.md` lists every file under `docs/`. |
| P14 | Each of the four package CIs has a job that runs `uv build`, installs the wheel into a clean venv, imports the top-level package, and runs the console script with `--help`. |
| P15 | `repo_boundary_check.py --mode enforce` passes, and `.agents/policy/repo-boundaries.json` contains no reference to an HTTP manager, `starlette`, `.agents/coordinator/`, or `.remote-dev/`. The checker's rules encode P1–P5 so that they are enforced in CI, not only at acceptance. |
| P16 | `pytest .agents/tests` and every skill `tests/` directory pass under both `uv run` and system `python3`. Every skill has a discoverable `tests/` directory and every one of them is in CI. |
| P17 | `rg -o '__VAWS_[A-Z_]+_PROGRESS__' .agents \| sort -u` yields at most one sentinel, and the four load-bearing skills emit Result Envelope v1 that passes `envelope_lint.py`. |
| P18 | A remote endpoint can request an unmultiplexed connection through remote-dev, and the analysis and collection long-stream paths use it rather than their own `ssh` invocation. |
| P19 | Both knowledge CLIs accept reader coordinates (the MCP tool already does); a query naming a `soc` that an entry's scope excludes does not return that entry, the response's `reader_coordinate` is populated, and a caller inside a run gets the derivable dimensions filled without passing them. |
| P20 | For every client, a config entry that a scaffold setup run reports as rewritten is absent from the written file afterwards, under both the hyphen and underscore spellings of the server name. |
| P21 | A named function converts a `remote-dev.result.v1` into a `parts` entry and into a `children` digest, and its tests cover all six remote-dev outcomes against both slots; every produced object passes `validate_envelope`, and a lossy outcome mapping keeps the original outcome in evidence. |
| P22 | No skill's private helper module spawns `ssh` or `tar`; `ascend-profiling-analysis` reaches hosts through remote-dev while its analysis modules are byte-identical to their pre-migration content. |

## 8. Out of scope for this round

- `ascend-profiling-analysis` **analysis logic**. Its `knowledge/`
  measurement data moves to `vaws-knowledge` as `measurement` entries (all
  `unverified`, per commons decision 24); the analyser reads them through the
  knowledge client with `include-unverified` and a built-in fallback.

  The boundary here needs stating precisely, because an earlier draft said
  "nothing else in the skill changes" and that contradicted P1 and P18. The
  skill has its own SSH and tar transport in its private `_common` module
  (`_ssh_base_cmd`, `ssh_stream` with keepalive options for long
  streams). P1 forbids scaffold Python from spawning `ssh`, and P18
  names this skill's long-stream path specifically. Those cannot hold while
  the skill keeps its own transport.

  So the split is transport versus analysis, not skill versus skill. The
  transport wrapper is **in scope this round**: that module is 770 of the
  skill's 44 178 lines, and the same pattern appears in ten skills, so it is
  one change made once rather than a special case. The remaining ~43 400
  lines of analysis logic are out of scope and are not read, moved or
  reshaped. What is deferred is the analyser; what moves now is how it
  reaches a host.
- Publishing the scaffold itself as a package.
- Any hosted or multi-user service.
- Compile-artifact → source mapping for operators (axiom 3 reserves the
  slot; nothing needs it yet).

## 9. Decisions recorded here

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
| 2026-09-09 | Superseded dated docs are deleted | Git is the archive; the 103 dead-path violations were all in `docs/audits/` |
| 2026-09-09 | `ascend-profiling-analysis` deferred at the analysis/transport line | owner deferred the skill; its 770-line SSH and tar transport still has to move, because P1 and P18 cannot hold otherwise and the same wrapper exists in ten skills |
| 2026-09-09 | Skills adopt Result Envelope v1 rather than the envelope being deleted | it currently has zero producers and eleven competing sentinels; a contract with no producers is not a contract, and the attribution/evidence structure is what agents need |
| 2026-09-09 | Unmultiplexed SSH stays an endpoint option | long-stream `mux=False` is a recorded failure signature, not an accident |
| 2026-09-09 | Reader coordinates become reachable from both CLIs and are auto-filled where derivable | the MCP tool in v0.1.3 already accepts them; the gap is CLI exposure plus population from the Run Manifest and machine profile. No second MCP surface |
| 2026-09-09 | The interpreter shim applies to inventoried non-`payload` entry points only; tests are not entries; unclassified `__main__` files are classification gaps | remote payloads run on the container with no `.venv` and no `.agents/lib`; putting the shim in every tracked `__main__` would re-exec 80+ pytest files the spec did not intend |
