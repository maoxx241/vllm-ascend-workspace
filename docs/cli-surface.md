# CLI surface: inventory, classification, target design, migration

Status: design document. This branch changes no behaviour; it adds the
inventory tool `.agents/scripts/cli_surface_inventory.py` and this document.

Regenerate the numbers and the table at the end with:

```bash
python3 .agents/scripts/cli_surface_inventory.py --format summary   # counts only
python3 .agents/scripts/cli_surface_inventory.py --format markdown  # the table below
python3 .agents/scripts/cli_surface_inventory.py                    # full JSON
```

The unit test `.agents/tests/test_cli_surface_inventory.py` holds the
repository to the figures in this document; when a script is added, moved or
removed, the classification overlay in the tool and the tables here must be
updated together.

## 1. Headline

| Measure | Value |
|---|---|
| Entry points (definition in §2) | **132** |
| ... of which an agent can be routed to today | 114 |
| Files importing `argparse` (non-test) | 91 |
| Skills that ship at least one entry point | 23 of 24 |
| Option strings across all entry points | 1 146 (428 distinct spellings) |
| Verbs (subcommands) | 116 |
| Spellings of "timeout" | 8 (`--timeout`, `--timeout-ms`, `--connect-timeout-ms`, `--health-timeout`, `--remote-timeout`, `--request-timeout`, `--profile-control-timeout`, `--analyse-timeout`) |
| Vocabularies for "which remote" | 3 (`--machine` ×45, `--session-id`/`--session-file` ×57/55, `--host`/`--port`/`--alias` ×23/25/21) |
| Independent SSH transport implementations | 9 |
| Independent `npu-smi` parsers | 7 |
| Job stores that do not know each other's job ids | 2 |
| Classification | mechanics 81 · judgment 8 · mixed 8 · redundant 35 |
| Proposed agent-facing commands | **13 nouns**, 75 verbs, replacing 114 agent-facing entry points |

The task brief measured 97 argparse programs (`.agents/skills/` 79,
`.agents/scripts/` 10, `.remote-dev/` 4). That count is right for what it
counted; it undercounts the surface because 36 entry points do not import
`argparse` at all: 18 `.agents/scripts/remote_*.py` shims delegate to parsers
inside `.agents/lib/vaws_remote_toolbox.py`, and 18 `.remote-dev/tools/*.py`
shims delegate to `.remote-dev/tools/_cli.py`. Both families are real,
agent-invocable commands with their own option lists; §2 defines the count so
that they are included and the number is reproducible.

## 2. Method

An **entry point** is a Python file that

- is tracked (or untracked and not ignored) outside `vllm/`, `vllm-ascend/`,
  `.git/`, `.vaws-local/`, `.remote-dev/state/`;
- is not a test (`tests/` directories, `test_*.py`, `selftest_*.py`,
  `conftest.py`) and not `__init__.py`;
- has an `if __name__ == "__main__":` guard or is a `__main__.py`.

For each one the tool extracts, with `ast` and without importing the code:
parser style (`argparse` inline, `delegated` to a library function, `bare`
`sys.argv`), verbs, option strings (including helper-added ones such as
`add_target_args(parser)` and the `if tool == "bash":` branches of
`_cli.build_parser`), the first docstring line, and every file that mentions
the script's basename, classified as routing document, skill document, script,
hook, MCP/coordinator code, generated mirror, client config, or test. When two
entry points share a basename (`remote_probe.py`, `remote_job_status.py`,
...), a mention is attributed by the directory attached to the basename; a bare
basename is attributed to both and marked as a collision.

Reference scanning is evidence, not proof of dependency. The classification
below was produced by reading each script, and several dependencies are only
visible that way: `collect_torch_profile_case.py` *imports*
`profile_control.py` and `run_remote_analyse.py`; `vaws_remote_toolbox.py`
*spawns* `parity_sync.py`, `remote_code_parity.py`, `serve_start.py`,
`serve_status.py` and `serve_stop.py` by path; `_workflow_common.py` imports
`inventory.py` and `manage_machine.py`; `modelscope_auto.py` spawns
`download_from_modelscope.py` and `verify_modelscope_sha256.py`. Those are the
cases where grep at the call site shows nothing and a rename would still break
a caller.

## 3. Classification

Four categories, defined by the principle in the brief:

- **mechanics** — deterministic, has a solved shape, belongs in the small
  consolidated surface;
- **judgment** — a decision wearing a CLI costume; should not be a command;
- **mixed** — the seam between a mechanical half and a judgment half is
  identified;
- **redundant** — another entry point already owns the mechanic.

Every entry point's category, target and one-line evidence note live in
`CLASSIFICATION` inside the inventory tool and in the table in §10. This
section gives the reasoning per group.

### 3.1 Judgment in a CLI costume (8) — and why they exist

Nine scripts across eight skills share one shape, and it is the shape that
gives them away:

| Script | Verbs | What `record`/`ingest` consumes | What `analyze`/`finalize` computes |
|---|---|---|---|
| `vllm-ascend-change-validation/scripts/change_validation.py` | plan · link · finalize | child Run Manifests | required-coverage tally → passed/failed/inconclusive |
| `vllm-ascend-performance-regression/scripts/performance_regression.py` | plan · record · normalize · analyze | agent-supplied benchmark result JSON | `degradation > max_relative_regression` per metric |
| `vllm-ascend-distributed-debug/scripts/distributed_debug.py` | init · ingest · analyze | agent-normalized event rows | fixed finding rules (participant mismatch, enter-without-exit, phase divergence) |
| `ascend-operator-debug/scripts/operator_debug.py` | plan · record · analyze | `--result` JSON the agent wrote | status counts → diagnosed/inconclusive/passed |
| `ascend-triton-operator-development/scripts/triton_development.py` | plan · finalize | config | manifest transition |
| `ascend-triton-kernel-validation/scripts/triton_validation.py` | plan · record · analyze | `--result` JSON | status counts |
| `ascend-triton-kernel-optimization/scripts/triton_optimization.py` | plan · record · analyze | agent-supplied measurements | weighted improvement vs thresholds |
| `ascend-triton-workflow/scripts/triton_workflow.py` | plan · link · finalize | the three ledgers above | all-children-passed |
| `vllm-ascend-graph-debug/scripts/graph_debug_case.py` (mixed, see §3.2) | init · record · compare · finalize | experiments + JSONL snapshots | compare is real; the rest is a ledger |

Evidence, held as a unit test (`test_every_judgment_script_runs_nothing_remotely`):
none of the eight judgment scripts opens an SSH connection or calls the remote
toolbox. They never touch the NPU. The agent runs the experiment through
`remote.bash` or a serving/benchmark script, decides what the result *means*
("numerical_mismatch", "unsupported", "flaky_or_nondeterministic"), writes that
verdict into a JSON file, and then invokes `record` so the script can count
the verdicts it was handed. The tally is one line of reasoning the agent has
already done; the schema check of the JSON it wrote is the only mechanic, and
`run_manifest.py validate` already provides it.

`change_validation.py plan` is the sharpest case. It maps a diff to "required
evidence" with regexes from `references/validation-rules.yaml`, e.g.
`(?i)(custom_op|ops/|kernel)` → `operator:dtype-shape-layout` is *required*.
The skill's own `SKILL.md` step 3 then instructs the agent to "correct
false-positive or missing mappings before consuming NPU resources". The
script proposes, the agent decides, and the agent is then asked to `link` and
`finalize` so the script can re-derive a verdict from decisions the agent
made. Deciding what validation a diff requires is exactly the open-world
judgment the brief names; it should be a reference document of rules of thumb
and a list of the mechanics available to gather evidence, not a planner.

`performance_regression.py` is the clearest instance of "two skills each needed
the same mechanic": it claims to "control alternating A/B experiments" but
executes nothing, while `vllm-ascend-benchmark/scripts/bench_compare.py`
(742 lines, `call_serve_start`, per-state checkout in the container) actually
runs the alternating loop and prints the deltas. The regression skill exists
because it felt safer to script the *verdict*; the mechanic it needed already
existed next door.

Why these were built: each of the eight carries `plan` and `analyze` verbs
whose only purpose is to make the agent's decision look like a program output.
That is the "it felt safer to script a decision than to trust an agent with
it" pattern, and the cost shows: 8 scripts, 3 750 lines, 8 test files, 24
verbs, and zero hardware interaction. The capability does not disappear — the
SKILL.md keeps the case-matrix discipline, the result vocabulary
(`exact_match`, `token_divergence`, ...), and the acceptance criteria as
*guidance*; evidence linkage moves to `vaws manifest`.

### 3.2 Mixed (8) — where the seam is

| Script | Mechanical half (consolidates) | Judgment half (becomes freedom) |
|---|---|---|
| `vllm-ascend-graph-debug/scripts/graph_debug_case.py` | `compare`: numeric diff of eager vs graph JSONL snapshots with `atol`/`rtol` → `vaws bench compare --jsonl` | `init`/`record`/`finalize`: hypothesis ledger |
| `vllm-ascend-correctness-validation/scripts/correctness_run.py` | `init` renders the two commands; `compare` classifies outputs by tolerance → `vaws bench compare` | whether `numerical_difference_within_tolerance` is acceptable for this change |
| `vllm-ascend-benchmark/scripts/bench_compare.py` | multi-state checkout + serve + bench loop → `vaws bench run --state` | reading the delta table |
| `vllm-ascend-pd-serving/scripts/pd_serving.py` | `start`/`status`/`smoke`/`stop` spawn `serve_start.py` per group member → `vaws serve --group` | `plan`: 130 lines of connector-config validation encode deployment choices |
| `ascend-memory-profiling/scripts/mem_analyze.py` | parsing dumps into a breakdown table → `vaws profile memory analyze` | the attribution narrative and "cross-validation" verdicts |
| `ascend-profiling-analysis/scripts/ascend_profile/diagnostics.py` | evidence tables | claim generation from `diagnosis_rules.yaml` thresholds; the agent should weigh them, not inherit them |
| `.agents/scripts/knowledge_capture.py` | redaction, dedupe, atomic write → `vaws knowledge capture` | "verified" is asserted through flags by the agent |
| `curate-workspace-knowledge/scripts/knowledge_curate.py` | promote/merge/reject/deprecate file moves → `vaws knowledge curate` | which verb applies is the review |

### 3.3 Redundant (35) — and why they exist

Three distinct causes, with different remedies.

**A. The toolbox and the substrate implement the same verbs (17).** The
`.agents/scripts/remote_*.py` family predates `.remote-dev/`. Thirteen of its
eighteen wrappers have a same-named verb in `.remote-dev/tools/` and as an MCP
tool, with a different result envelope, a different target vocabulary
(`--machine/--session-id/--session-file` vs `--host/--port/--alias/...`), and
for jobs a different store: the toolbox keeps jobs under
`.vaws-local/remote-toolbox/jobs/` locally and `<toolbox-root>/jobs/`
remotely; the substrate keeps them under `<root>/.remote-dev/jobs/`. A job id
from one is unknown to the other. Three more toolbox wrappers
(`remote_sync_plan/apply`, `remote_service_start/status/stop`) are adapters that
spawn `parity_sync.py` or `serve_*.py` as subprocesses, re-parse their JSON
stdout and re-wrap errors — a second layer over one mechanic that makes the
failing layer harder to see, not easier. `remote_monitor.py` in the substrate is
`remote_bash(run_in_background=True)` under another name. Cause: two skills
(remote-toolbox, remote-dev) each owned a transport and neither wanted to
depend on the other.

**B. Libraries that grew a `__main__` (14).** `inventory.py` and
`manage_machine.py` (3.5k lines) are imported by `_workflow_common.py` and
documented as "low-level only", yet expose 5 and many verbs. `profile_control.py`
and `run_remote_analyse.py` are imported by `collect_torch_profile_case.py`
(`post_remote_action`, `analyse_profile_root`) and duplicate one step each of
the orchestrator as a CLI. Eight `ascend_profile/*` entries — six stages (`normalize`, `segment`,
`classify`, `summarize`, `report`, `cross_rank`) and two renderers
(`html_report`, `html_report_v2/__main__`) — each have a debug CLI while
`analyze.py` is the pipeline. `modelscope_download_status.py` is reimplemented
inside `modelscope_auto.py status`. `serve_probe_npus.py` is one of seven
`npu-smi` parsers. Cause: a stage was exposed for debugging and never
un-exposed. Remedy: drop the `__main__` blocks; the code stays as library.

**C. Tracked copies (4).** `.trae/skills/modelscope/scripts/*.py` are
byte-identical copies of the `.agents` scripts (verified with `diff`), while
`.claude/skills/*/SKILL.md` are *generated* shims maintained by
`sync_claude_skills.py --check`. The `.trae` mirror has no generator and no
check, so it will drift. Remedy: generate or delete; never hand-copy.

### 3.4 Mechanics (81) grouped by the mechanic they own

The mechanics are already clustered around a dozen nouns; the clustering is
what §5 turns into commands. Groups and today's entry-point counts (the full
mapping is in §10 and in the tool's `target_surface.collapse`):

| Mechanic | Today (mechanics + mixed) | Notes |
|---|---|---|
| Remote read/write/edit/search/shell/jobs/artifacts/probe | 17 (`.remote-dev/tools`) | already one implementation, 18 files |
| Machine attach/verify/repair/remove, fleet monitor | 5 | `_workflow_common` is the real library |
| Session create/list/status/remove/group/gc/diff, NPU leases, cleanup | 9 | `remote_cleanup.py` and `session_gc.py` overlap |
| Code parity sync, consent, cache gc, watch, runtime attest | 5 | plus `remote_code_parity.py` as payload |
| Service start/status/logs/stop, grouped PD | 5 (4 + 1) | `remote_service_logs.py` is the only log tailer |
| Benchmark run/compare, correctness harness, AISBench adapter | 4 (2 + 2) | plus one remote payload |
| Profiling collect/analyze/sweep/memory | 5 (4 + 1) | plus 4 payloads |
| Model weights ensure/status/verify | 1 | plus 2 subprocess payloads |
| Workspace identity/profile/init/topology/client config | 8 | all local file + `gh`/`git` |
| Run Manifest init/validate | 1 | consumed by every ledger |
| Knowledge validate/query/capture/curate | 4 (2 + 2) | sibling-owned files; classified, not touched |
| Repo self-checks | 5 | catalog, shims, scaffold, this inventory, Triton static gate |
| Pool task facade | 1 | `vaws.py`, already mirrored as MCP `vaws_*` |
| Not agent-invoked: hooks 4, servers 2, payloads 9, harnesses 3 | 18 | stay as they are, leave routing docs |

## 4. One mechanic, several entry points — where it is happening now

| Mechanic | Implementations | Where |
|---|---|---|
| SSH command execution | 9 | `.remote-dev/core/ssh_transport.py`, `.agents/lib/vaws_ssh.py`, `vaws_remote_toolbox.ssh_exec*`, `remote-code-parity/scripts/common.py`, `vllm-ascend-serving/scripts/_common.py`, `ascend-profiling-analysis/scripts/_common.py`, `ascend-memory-profiling/scripts/_common.py`, `vllm-ascend-benchmark/scripts/_common.py`, `machine-management/scripts/manage_machine.py`, `session-management/scripts/npu_coordination.py` |
| `npu-smi` parsing | 7 | `manage_machine.py` (16 sites), `mem_collect.py` (13), `mem_analyze.py`, `vaws_npu_coordination.py`, `serving/_common.py`, `session_create.py`, `serve_probe_npus.py` |
| Endpoint probe | 3 | `.agents/scripts/remote_probe.py`, `.remote-dev/tools/remote_probe.py`, MCP `remote_probe` (the substrate pair share code; the toolbox one does not) |
| Background jobs (start/status/tail/stop) | 2 stores × 2 CLIs + MCP | toolbox `remote_job_*` vs substrate `remote_job_*`/`remote.bash --run-in-background` |
| Artifact manifest/pull/push | 2 CLIs + MCP | toolbox vs substrate |
| Code sync | 3 layers | `remote_sync_apply.py` → `parity_sync.py` → `remote_code_parity.py sync`, each a subprocess with JSON re-parsing |
| Service lifecycle | 2 layers | `remote_service_start.py` → `serve_start.py` |
| Timeout flag | 8 spellings | see §1; the MCP `remote.bash` schema additionally accepts both `timeout` and `timeout_ms` |
| Target selection | 3 vocabularies | `--machine`, `--session-id/--session-file`, `--host/--port/--alias/--user/--root/--cwd` |

The knowledge base records the cost of exactly this layering: signature
`remote-toolbox-ssh-stream-through-the-shared-controlmaster-mux` notes the
ControlMaster fix had to be applied "in remote-code-parity + vllm-ascend-serving
ssh helpers" separately, and that an instant timeout from MCP `remote_bash` is
a tool-service fault, not a remote-command fault. With nine transports, a
transport bug is fixed nine times or diagnosed in the wrong layer.

The remote-dev CLI fallbacks are also a *dark* surface: eleven of the eighteen
`.remote-dev/tools/*.py` files are not named by any document or script outside
tests (`unreferenced_outside_tests` in the tool output). Agents are routed to
the MCP tools; the per-tool CLI files exist for completeness and their argument
lists in `_cli.py` are a hand-maintained mirror of `.remote-dev/mcp/schemas.py`.

## 5. Target surface

### 5.1 Shape

One root program, `vaws`, thirteen nouns, verbs under each. `.agents/scripts/vaws.py`
already exists as the pool-task facade and becomes the root; the existing
`attach/session/run/execution/finish` verbs move under `vaws task` unchanged.

Global contract, identical for every noun and verb:

| Element | Rule |
|---|---|
| Target | exactly one flag, `--target <spec>`, where spec is `session:<id>`, `machine:<alias>`, `alias:<endpoint-alias>`, or `<host>:<port>`; default is the cwd worktree binding. Resolved once, by `core.endpoint.resolve_endpoint`, for every command. `--session-id`, `--session-file`, `--machine`, `--host`, `--port` are accepted as deprecated aliases during the compatibility period and rewritten to `--target` before dispatch. |
| Timeout | `--timeout <seconds>` (float). No millisecond spellings. Phase-specific timeouts become `--timeout-<phase>` only where a phase is genuinely separate (health wait, analyse), never a synonym. |
| Output | `--output-dir <path>` for anything that writes artifacts; `--dry-run` where a mutation exists; `--force` where a stop/remove exists. |
| Stdout | one JSON object, the result envelope in §6. |
| Stderr | bounded phase progress, `[<noun>.<verb>] <phase>: <detail>`. |
| Exit code | `0` success, `1` failed, `2` needs_input (bad or missing arguments, unresolved target, consent required). Never a traceback. |
| Passthrough | `-- <args>` forwards to the wrapped program (`vllm serve`, `vllm bench serve`) and is echoed back in the envelope. |

### 5.2 Commands

Each command is justified by the mechanic it owns and lists what collapses into
it. Counts are entry points from §10; "verbs" are the proposed verbs.

**`vaws remote`** — the substrate. Owns SSH transport, remote file operations,
remote shell, background jobs, artifacts, probe. Verbs: `read write edit
multi-edit ls glob grep bash job-status job-tail job-stop artifact-manifest
artifact-pull artifact-push probe snapshot` (15). MCP-first: these are the
`remote_*` MCP tools. The CLI is *one* dispatcher generated from
`.remote-dev/mcp/schemas.py`, not eighteen files with a hand-mirrored parser.
Collapses 17 substrate files plus, as retired duplicates, 11 toolbox wrappers
(`remote_target_resolve`, `remote_probe`, `remote_exec`, `remote_job_start`,
`remote_job_status`, `remote_job_tail`, `remote_job_stop`, `remote_job_collect`,
`remote_artifact_manifest`, `remote_artifact_pull`, `remote_artifact_push`) and
`remote_monitor` (= `bash --background`). This is also the only SSH transport;
the other eight become callers of `.remote-dev/core/ssh_transport.py`.

**`vaws machine`** — a host in the fleet. Verbs: `add verify repair remove
probe monitor` (6). `probe` is the one `npu-smi` parser (host-side, all
containers visible) and replaces `serve_probe_npus.py` and the six other
parsers. `monitor` absorbs `manage_monitor.py ensure/status/restart/stop` as
`monitor --ensure|--status|--restart|--stop`. Collapses 5 mechanics + 3
redundant (`inventory.py`, `manage_machine.py`, `serve_probe_npus.py`). Image
track stays an explicit user gate exactly as today.

**`vaws session`** — isolated worktree + container + leases. Verbs: `create
list status remove group diff gc lease` (8). `gc` merges `session_gc.py` and
`remote_cleanup.py` (jobs, services, leases, known-hosts, temp; all
dry-run-capable). `lease` is `npu_coordination.py`'s eleven verbs as
`lease <submit|acquire|preflight|activate|heartbeat|release|cancel|status|gc|hold-add|hold-remove>`;
it remains optional and never a gate. Collapses 9.

**`vaws sync`** — make the container's code tree match the local worktree.
Verbs: `apply plan consent gc watch attest` (6). `plan` = `parity_sync --dry-run`.
`attest` is `prepare_runtime.py` (attest/publish/restore of a built runtime).
`remote_code_parity.py` stays as the implementation payload and loses its
routing entry. Collapses 5 mechanics + 2 redundant toolbox adapters
(`remote_sync_plan`, `remote_sync_apply`) by removing the middle subprocess
layer.

**`vaws serve`** — the vLLM service lifecycle on a session. Verbs: `start
status logs stop` (4) with `--group <group-id>` for prefill/decode groups
(from `pd_serving.py start/status/smoke/stop`; `smoke` becomes `status --smoke`).
`logs` comes from `remote_service_logs.py`, the only log tailer. Collapses 4
mechanics + 1 mixed (`pd_serving`) + 3 redundant toolbox adapters
(`remote_service_start/status/stop`).
The PD connector *plan* is not a verb; it is a reference document plus the
config file the agent writes.

**`vaws bench`** — run a workload and compare outputs or metrics. Verbs: `run
compare correctness aisbench` (4). `run --state <ref>...` is `bench_compare.py`'s
multi-state loop; `run` without states is `bench_run.py`. `compare` is the one
numeric/JSONL/metric comparator (from `correctness_run.py compare` and
`graph_debug_case.py compare`) and prints deltas and classes, never a verdict.
`correctness` renders and runs the offline/online harness
(`remote_correctness_harness.py` stays as remote payload). Collapses 2
mechanics (`bench_run`, `aisbench_adapter`) + 2 mixed (`bench_compare`,
`correctness_run`) and absorbs the `compare` half of `graph_debug_case`.

**`vaws profile`** — collect and analyse profiler data. Verbs: `collect analyze
sweep memory-collect memory-analyze` (5). `collect` is
`collect_torch_profile_case.py`; `profile_control.py` and `run_remote_analyse.py`
lose their `__main__` and stay imported. `analyze`/`sweep` are the local
drivers; `ascend_profile/analyze.py` and `sweep.py` remain remote payloads and
the seven stage CLIs lose their `__main__`. `memory-analyze` emits the
breakdown table; the attribution narrative in `mem_analyze.py` becomes
reference text. Collapses 4 mechanics + 1 mixed + 10 redundant, and keeps 4
payloads (`analyze`, `sweep`, `diagnostics`, `weight_inspector`).

**`vaws model`** — model weights. Verbs: `ensure status verify` (3), from
`modelscope_auto.py`; the download and sha256 scripts stay as subprocess
payloads. Collapses 1 + 5 redundant (incl. the `.trae` copies).

**`vaws workspace`** — local facts and state. Verbs: `identity profile init
topology ci-pin client gh-install` (7). Collapses 8: `workspace_identity`,
`workspace_profile`, `repo_init_probe`, `repo_init_profile`, `repo_topology`,
`resolve_vllm_ci_pin`, `vaws_client_setup`, `install_gh_user`. The username
choice remains a user gate.

**`vaws manifest`** — Run Manifest v1. Verbs: `init validate link` (3). `link`
is the one evidence-linking mechanic the eight ledgers each re-implemented
(`link`/`record` of a child manifest). Collapses 1 and absorbs the only
mechanical residue of the judgment scripts.

**`vaws knowledge`** — the formal knowledge store. Verbs: `validate query
capture curate` (4). Sibling-owned files today; listed so the surface is
complete. Collapses 4.

**`vaws lint`** — repo self-checks, no remote. Verbs: `skills cli-surface shims
scaffold triton` (5): `skill_catalog.py`, this tool, `sync_claude_skills.py
--check`, `validate_remote_dev_scaffold.py --local-only`, `validate_triton_impl.py`.
Collapses 5.

**`vaws task`** — the pool task facade, unchanged: `attach session run
execution finish` (5). MCP-first (`vaws_session`, `vaws_run`, `vaws_execution`,
`vaws_finish`). Note the naming collision that already exists: `vaws.py session`
is a pool binding, `session_create.py` is a worktree+container. Keeping both
under distinct nouns (`task` vs `session`) is the least disruptive fix.

Total: 13 nouns, 75 verbs (`session lease` counted once; it carries the
eleven coordination sub-verbs). Today's agent-facing surface is 114 entry points,
114 verbs and 1 061 option strings; the 8 judgment ledgers (26 verbs) leave
the surface entirely and the rest lose their duplicate spellings.

### 5.3 MCP or CLI

Rule: a mechanic is an MCP tool when it completes in one bounded round-trip
without phases (read, write, edit, grep, a ≤2-minute shell command, job
status, probe). Everything with phases, progress, consent or minutes of runtime
(machine add, session create, sync, serve start, bench, profile) is a CLI the
agent runs through its native shell tool, where stderr progress is visible and
an MCP timeout cannot masquerade as a remote failure. That is already how the
repo behaves; the rule makes it explicit and stops the toolbox from
re-growing CLI copies of the MCP tools.

Where the same mechanic is on both surfaces today the CLI must be *generated*
from the MCP schema (`vaws remote`), or the drift already visible in
`_cli.py` versus `schemas.py` (`timeout` vs `timeout_ms`, `--edits-json` vs
`edits`) returns.

## 6. Diagnosability contract

Every command returns the `remote-dev.result.v1` envelope, extended with two
fields that today are missing from the toolbox and skill scripts:

```json
{
  "tool": "vaws.serve.start",
  "outcome": "failed",
  "status": "health_timeout",
  "summary": "…",
  "target": {"kind": "session", "session_id": "…", "endpoint_id": "…", "container": "…"},
  "command": ["ssh", "-p", "…", "root@…", "bash -lc '…'"],
  "layer": "remote-service",
  "logs": {"local": ".vaws-local/serving/<id>/start.log", "remote": "/vllm-workspace/…/serve.log"},
  "duration_ms": 91234,
  "refs": {"manifest": "…"}
}
```

`layer` is one of `local-cli`, `endpoint-resolution`, `ssh-transport`,
`remote-command`, `remote-service`, `artifact-transfer`. It is set by the
component that raised, not inferred by the caller. `command` is the exact argv
that was executed (with secrets redacted), so a failure can be reproduced
without re-running the wrapper. With the subprocess layers of §4 removed there
is one place per command that can set `layer` correctly.

## 7. Sequenced migration plan

Every phase is one or more PRs; none deletes a file that another phase has not
first stopped calling. The inventory tool's `entry_point_count`,
`by_category.redundant` and `unreferenced_outside_tests` are the acceptance
numbers for each phase and are asserted in the test, so a phase cannot land
half-done without failing CI.

**Phase 0 — this branch.** Inventory tool, document, test. No behaviour change.

**Phase 1 — one transport, one envelope (`.remote-dev/**`, sibling-owned).**
Add `layer` and `command` to `remote-dev.result.v1`. Generate `vaws remote
<tool>` from `schemas.py`; keep the 18 `.remote-dev/tools/*.py` files as
one-line shims to it. Expose `core.ssh_transport` as the transport the toolbox
library and the five skill `_common.py` files import. Nothing outside
`.remote-dev/` changes yet; the count is unchanged (shims still count) but
`_cli.py`'s hand-mirrored parser disappears. Breaks nothing.

**Phase 2 — retire the toolbox duplicates (`remote-toolbox` package as a
whole).** Turn the 13 duplicate `remote_*.py` wrappers into deprecation shims:
print `{"deprecated": true, "replacement": "vaws remote …"}` on stderr, exec
the replacement, return its stdout. Update together, per the maintenance rule:
`remote-toolbox/SKILL.md`, its `references/`, `.agents/README.md` "Current
primary helpers" list, `.agents/tests/test_vaws_scaffold_safety.py`. Keep
`vaws_remote_toolbox.py` as a library: `.remote-dev/core/endpoint.py`, five
`_common.py` files and `npu_coordination.py` import it. What breaks if done out
of order: the toolbox job store and the substrate job store hold different job
ids, so `remote_job_status` shims must translate or refuse with `needs_input`
rather than silently query the wrong store. Count: redundant 35 → 19.

**Phase 3 — the `vaws` root, one package at a time, in dependency order.**
`.agents/scripts/vaws.py` gains nouns; each verb calls the existing script's
`main(argv)` (38 skill scripts already expose it; ~20 use `main()` and need
the one-line signature change). The old script becomes a shim. Order is
dictated by who spawns whom:

1. `workspace` (no dependents) and `lint`.
2. `machine` (depends on workspace profile) — update `machine-management` and
   `npu-fleet-monitor` packages; `serve_probe_npus.py` becomes a shim to
   `vaws machine probe`; **AGENTS.md line 107 changes** (it names
   `serve_probe_npus.py`).
3. `session` (depends on machine) — `session-management` package;
   `npu_coordination.py` → `vaws session lease`; **AGENTS.md line 107 changes**
   (names `session_create.py`, `npu_coordination.py`).
4. `sync` (depends on session) — `remote-code-parity` package. `parity_sync.py`
   is spawned by path from `serve_start.py`, `vaws_remote_toolbox.py`,
   `mem_collect.py`, `profile_analyze.py`, `profile_sweep.py`; those five
   callers switch to `vaws sync apply` in the same PR, then `parity_sync.py`
   becomes a shim. `remote_code_parity.py` is never renamed (payload).
   **AGENTS.md line 107 changes** (names `parity_sync.py`).
5. `serve` (depends on sync, session) — `vllm-ascend-serving` and
   `vllm-ascend-pd-serving` packages. `serve_start.py` is spawned by
   `pd_serving.py`, `bench/_common.py`, `collect_torch_profile_case.py`,
   `mem_collect.py`, toolbox; same rule.
6. `bench`, `profile`, `model` (depend on serve) — their packages.
7. `manifest`, `knowledge` (sibling-owned; only the shim and the routing line).
8. `task` — rename in place, no callers change.

Each step's PR updates that skill's `SKILL.md`, `references/`, `scripts/`,
`agents/`, `tests/`, regenerates `.claude/skills` shims with
`sync_claude_skills.py`, and carries the exact `AGENTS.md` / `.agents/README.md`
line replacements (§9). The `.trae/skills/*` mirror has no generator; step 6
deletes the four copies or adds a generator, and `.trae/rules/project_rules.md`
is checked for script names.

**Phase 4 — judgment becomes guidance.** For each of the eight skills: delete
the ledger script and its test; move the result vocabulary, case-matrix
discipline and acceptance criteria into `references/`; point evidence linking
at `vaws manifest link`; point numeric comparison at `vaws bench compare`.
`graph_debug_case.py compare` moves into `vaws bench compare --jsonl` *before*
the script is deleted. `validation-rules.yaml` stays as a reference table the
agent reads, not a planner input. Count: judgment 8 → 0, mixed 8 → ~4.

**Phase 5 — drop `__main__` from libraries.** `inventory.py`,
`manage_machine.py`, `profile_control.py`, `run_remote_analyse.py`, eight
`ascend_profile/*` entries, `modelscope_download_status.py`. Pure deletion of
guard blocks; the tool's count drops by 13 and confirms nothing else referenced
them.

**Compatibility period.** Shims live for the duration of Phase 3 plus one
full cycle of the maturation harness on real hardware (§8). A shim is removable
when the inventory tool shows zero `skill-doc`, `routing`, `script` or `mirror`
references to it — `unreferenced_outside_tests` is the removal list. Shims
never change semantics: same flags accepted, same JSON on stdout, one added
stderr line. Hooks (`.agents/hooks`, `.remote-dev/hooks`) are untouched
throughout; they are not part of the agent surface.

**What breaks if the order is violated.** Renaming `parity_sync.py` before
step 4 breaks `serve start`, every profiling collection and the toolbox sync
adapter at once, and the failure surfaces as `FileNotFoundError` inside a
subprocess three layers down — the exact wrong-layer diagnosis the brief warns
about. Renaming `serve_start.py` before step 5 breaks PD serving, benchmarks
and profiling. Deleting a judgment script before its `SKILL.md` is rewritten
leaves a routing rule pointing at nothing (`skill_catalog.py` catches the
dead link).

**Riskiest consolidation.** `vaws sync`. It is the only mechanic that every
other remote mechanic depends on (serve, bench, profile and the pool task
client all spawn `parity_sync.py` or `remote_code_parity.py`), it has three
callers that are not skills (`vaws_remote_toolbox.py`, `vaws_task_client.py`,
`prepare_runtime.py`), it carries a consent gate whose state file must not be
re-keyed, and its correctness is only observable on a real container (the
synthetic-ref mirror and the materialize/install modes). A regression there
does not fail loudly; it serves stale code. Second: unifying the two job
stores, because in-flight job ids from the toolbox store become unreachable
the moment the shim points at the substrate store unless the shim translates.

## 8. Real-hardware validation per consolidated command

A command is mature when the following has been demonstrated against real
remote hosts by the maturation harness (owned by a sibling agent; not run
here). "Envelope" means the §6 result with `layer` set correctly.

| Command | Must be proven on real hosts | Evidence artifact |
|---|---|---|
| `vaws remote` | every verb against an A2 and an A3 container; `bash` with a 10-minute command over the ControlMaster mux and off it (the known-failure signature); `job-*` across a dropped SSH connection; `artifact-pull` of ≥1 GiB with sha256 match; `probe` on an unreachable host returns `layer=ssh-transport` within `--timeout` | harness run manifest + envelope corpus, one per verb × host |
| `vaws machine` | `add` on a fresh host with each image track (`local-latest`, `rc`, `main`, `stable`, custom); `verify` on a host with mixed occupied/free NPUs matches `npu-smi` by eye; `repair` is idempotent (run twice, second is a no-op); `remove` leaves no container; `probe` sees processes from other containers | before/after `docker ps`, `npu-smi info` captures |
| `vaws session` | two agents create sessions on one host concurrently and get disjoint NPUs; `remove` under an alive service; `gc --reap-dead` never releases a lease whose device is still busy; `lease` queue across two workspaces | lease files + host `npu-smi` traces |
| `vaws sync` | source-only, materialize and install modes each from a dirty worktree with submodule changes; `plan` matches `apply`'s actual actions; `watch` republishes within one interval; consent refused → exit 2 with `needs_input`, nothing written | remote `git log` in the cache root, consent file, envelopes |
| `vaws serve` | start/status/logs/stop for one dense and one MoE model; `--group` prefill/decode start, smoke, stop, and rollback when the decode member fails; stop with orphaned worker processes (`--force`); `logs` while the mux is saturated | service state files, `/health` traces, log tails |
| `vaws bench` | `run` warm multi-run with warmup exclusion reproduces within noise across two invocations; `run --state a --state b` produces deltas attributable to code only (identical serve config recorded); `compare` on eager vs graph JSONL with known injected divergence flags exactly that record; `correctness` offline and online modes | result JSON pairs, comparison tables |
| `vaws profile` | `collect` for a service with `--profiler-config`, manifest verified; `analyze` on the produced root reproduces the golden `kernel_details.csv` equivalence (`golden_db_vs_csv.py`); `sweep` over ≥3 roots; `memory-collect` phases 0–2 with `npu-smi` deltas | manifests, golden report, breakdown CSVs |
| `vaws model` | `ensure` resumes a killed download; `verify` detects one corrupted shard; `status` sizes match `du` | sha256 report, resume log |
| `vaws workspace` | no hardware; `gh` auth present/absent, submodule states, topology configure on a fork | local JSON only |
| `vaws manifest` | no hardware; schema round-trip and `link` on manifests produced by every other command above | validated manifests |
| `vaws knowledge` | no hardware; sibling-owned | — |
| `vaws lint` | no hardware; CI | — |
| `vaws task` | one pool binding: attach → session → run → execution status/tail/stop → finish on a real coordinator | coordinator ledger |

Two properties are common to all: (1) every failure envelope in the corpus
names the correct `layer` — checked by injecting one fault per layer
(unresolvable target, refused SSH, non-zero remote command, unhealthy service,
truncated artifact); (2) `--timeout` is honoured within 10 % for every verb
that takes it.

## 9. Routing lines (not applied here; for the PRs that own them)

`AGENTS.md` and `.agents/README.md` are sibling-owned. These are the exact
lines Phase 3 replaces; they are reproduced in the PR description.

`AGENTS.md`, "Repo-wide rules", the line beginning "Remote work runs inside a
`session-management` session" — replace the final sentence

> Legacy compatibility surfaces still accept `--machine`: `remote-code-parity/scripts/parity_sync.py`, `session-management/scripts/npu_coordination.py`, and `vllm-ascend-serving/scripts/serve_probe_npus.py`.

with

> Every `vaws` command takes one target flag, `--target session:<id>|machine:<alias>|<host>:<port>`, defaulting to the cwd worktree binding; `--session-id`, `--session-file` and `--machine` are deprecated aliases that are rewritten to `--target`.

`AGENTS.md`, add after "Skill wrappers: progress on `stderr`, final JSON on `stdout`.":

> - Deterministic mechanics are invoked through `vaws <noun> <verb>` (`remote`, `machine`, `session`, `sync`, `serve`, `bench`, `profile`, `model`, `workspace`, `manifest`, `knowledge`, `lint`, `task`). Skill scripts under `.agents/skills/*/scripts/` that print a `deprecated` notice are shims; use the replacement they name. Do not add a new argparse program to a skill; add a verb or write guidance. `python3 .agents/scripts/cli_surface_inventory.py --format summary` must report zero findings.

`AGENTS.md`, the knowledge lines (103 and 105) — replace
`.agents/scripts/knowledge_query.py` with `vaws knowledge query` and
`.agents/scripts/knowledge_capture.py` with `vaws knowledge capture` when
Phase 3 step 7 lands.

`.agents/README.md`, "Current primary helpers" — replace the per-script list with:

> - `.agents/scripts/vaws.py` — the consolidated CLI (`vaws <noun> <verb>`); see `docs/cli-surface.md`
> - `.agents/scripts/cli_surface_inventory.py` — entry-point inventory and drift check
> - `.agents/tests/test_vaws_scaffold_safety.py`

and append to "Script-first convention":

> One mechanic, one entry point. If a mechanic is an MCP tool (`remote_*`, `vaws_*`), its CLI form is generated from the MCP schema, never hand-written.

## 10. Inventory table

Generated by `python3 .agents/scripts/cli_surface_inventory.py --format markdown`.
"Refs" counts referencing files by kind outside the file itself. "Target" is
the consolidated command, a non-command kind (`guidance`, `payload`, `hook`,
`server`, `harness`), or for redundant rows the surviving entry point.

| Entry point | Style | Verbs | Options | Refs | Category | Target |
|---|---|---|---|---|---|---|
| `.agents/coordinator/prepare_runtime.py` | argparse | attest, publish, restore | 5 | routing:1 | mechanics | vaws sync |
| `.agents/coordinator/server.py` | argparse | - | 3 | routing:1, test:1 | mechanics | server |
| `.agents/hooks/knowledge_session_end.py` | bare | - | 0 | client-config:1, routing:1, test:2 | mechanics | hook |
| `.agents/hooks/vaws_session.py` | argparse | - | 2 | script:1, test:1 | mechanics | hook |
| `.agents/scripts/cli_surface_inventory.py` | argparse | - | 3 | test:1 | mechanics | vaws lint |
| `.agents/scripts/knowledge_capture.py` | argparse | - | 6 | routing:2, skill-doc:2, test:2 | mixed | vaws knowledge |
| `.agents/scripts/knowledge_query.py` | argparse | - | 6 | routing:2, test:2 | mechanics | vaws knowledge |
| `.agents/scripts/knowledge_validate.py` | argparse | - | 1 | other:2, routing:1, skill-doc:1, test:1 | mechanics | vaws knowledge |
| `.agents/scripts/remote_artifact_manifest.py` | delegated | - | 4 | routing:1, skill-doc:3 | redundant | .remote-dev/tools/remote_artifact_manifest.py |
| `.agents/scripts/remote_artifact_pull.py` | delegated | - | 5 | routing:1, skill-doc:4 | redundant | .remote-dev/tools/remote_artifact_pull.py |
| `.agents/scripts/remote_artifact_push.py` | delegated | - | 5 | routing:1, skill-doc:1 | redundant | .remote-dev/tools/remote_artifact_push.py |
| `.agents/scripts/remote_cleanup.py` | delegated | - | 13 | routing:1, skill-doc:3 | mechanics | vaws session |
| `.agents/scripts/remote_exec.py` | delegated | - | 8 | routing:1, skill-doc:2 | redundant | .remote-dev/tools/remote_bash.py |
| `.agents/scripts/remote_job_collect.py` | delegated | - | 5 | routing:1, skill-doc:1 | redundant | .remote-dev/tools/remote_artifact_pull.py |
| `.agents/scripts/remote_job_start.py` | delegated | - | 10 | other:1, routing:1, skill-doc:3 | redundant | .remote-dev/tools/remote_bash.py |
| `.agents/scripts/remote_job_status.py` | delegated | - | 4 | routing:1, skill-doc:2, test:1 | redundant | .remote-dev/tools/remote_job_status.py |
| `.agents/scripts/remote_job_stop.py` | delegated | - | 5 | routing:1, skill-doc:1 | redundant | .remote-dev/tools/remote_job_stop.py |
| `.agents/scripts/remote_job_tail.py` | delegated | - | 6 | routing:1, skill-doc:2 | redundant | .remote-dev/tools/remote_job_tail.py |
| `.agents/scripts/remote_probe.py` | delegated | - | 4 | routing:1, skill-doc:3, test:1 | redundant | .remote-dev/tools/remote_probe.py |
| `.agents/scripts/remote_service_logs.py` | delegated | - | 4 | routing:1, skill-doc:1 | mechanics | vaws serve |
| `.agents/scripts/remote_service_start.py` | delegated | - | 3 | routing:1, skill-doc:3 | redundant | .agents/skills/vllm-ascend-serving/scripts/serve_start.py |
| `.agents/scripts/remote_service_status.py` | delegated | - | 3 | routing:1, skill-doc:1 | redundant | .agents/skills/vllm-ascend-serving/scripts/serve_status.py |
| `.agents/scripts/remote_service_stop.py` | delegated | - | 4 | routing:1, skill-doc:1 | redundant | .agents/skills/vllm-ascend-serving/scripts/serve_stop.py |
| `.agents/scripts/remote_sync_apply.py` | delegated | - | 6 | routing:1, skill-doc:5 | redundant | .agents/skills/remote-code-parity/scripts/parity_sync.py |
| `.agents/scripts/remote_sync_plan.py` | delegated | - | 5 | routing:1, skill-doc:6 | redundant | .agents/skills/remote-code-parity/scripts/parity_sync.py |
| `.agents/scripts/remote_target_resolve.py` | delegated | - | 3 | routing:1, skill-doc:3 | redundant | .remote-dev/tools/remote_probe.py |
| `.agents/scripts/remote_toolbox_stress.py` | argparse | - | 13 | routing:1, skill-doc:1 | mechanics | harness |
| `.agents/scripts/run_manifest.py` | argparse | init, validate | 10 | other:2, routing:2 | mechanics | vaws manifest |
| `.agents/scripts/skill_catalog.py` | argparse | - | 2 | other:1, test:1 | mechanics | vaws lint |
| `.agents/scripts/vaws.py` | argparse | attach, session, run, execution, finish | 12 | other:1, routing:1, test:1 | mechanics | vaws task |
| `.agents/scripts/vaws_client_setup.py` | argparse | - | 4 | routing:1, skill-doc:2, test:1 | mechanics | vaws workspace |
| `.agents/scripts/workspace_identity.py` | argparse | summary, ensure, validate-alias, set-alias, decline-alias | 1 | routing:1, skill-doc:5 | mechanics | vaws workspace |
| `.agents/scripts/workspace_profile.py` | argparse | summary, validate, ensure | 4 | routing:2, script:1, skill-doc:8 | mechanics | vaws workspace |
| `.agents/skills/ascend-memory-profiling/scripts/mem_analyze.py` | argparse | - | 1 | routing:1, skill-doc:1 | mixed | vaws profile |
| `.agents/skills/ascend-memory-profiling/scripts/mem_collect.py` | argparse | - | 25 | routing:1, script:1, skill-doc:2, test:1 | mechanics | vaws profile |
| `.agents/skills/ascend-memory-profiling/scripts/weight_inspector.py` | argparse | - | 0 | routing:1, script:1, skill-doc:1 | mechanics | payload |
| `.agents/skills/ascend-operator-debug/scripts/operator_debug.py` | argparse | plan, record, analyze | 3 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py` | argparse | - | 14 | routing:1, script:2, skill-doc:9, test:1 | mechanics | payload |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/classify.py` | argparse | - | 1 | skill-doc:3 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/cross_rank.py` | argparse | - | 1 | skill-doc:4, test:1 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/diagnostics.py` | argparse | - | 1 | script:3, skill-doc:6, test:2 | mixed | payload |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report.py` | bare-argv | - | 0 | skill-doc:2 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report_v2/__main__.py` | bare | - | 0 | test:1 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/normalize.py` | argparse | - | 4 | skill-doc:2 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/report.py` | argparse | - | 6 | script:3, skill-doc:4, test:2 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/segment.py` | argparse | - | 3 | script:2, skill-doc:8, test:1 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/summarize.py` | argparse | - | 8 | skill-doc:5, test:1 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/sweep.py` | argparse | - | 13 | routing:1, script:2, skill-doc:4 | mechanics | payload |
| `.agents/skills/ascend-profiling-analysis/scripts/dev/golden_db_vs_csv.py` | argparse | - | 4 | skill-doc:2 | mechanics | harness |
| `.agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py` | argparse | - | 25 | routing:1, script:1, skill-doc:5, test:1 | mechanics | vaws profile |
| `.agents/skills/ascend-profiling-analysis/scripts/profile_sweep.py` | argparse | - | 16 | routing:1, script:1, skill-doc:4 | mechanics | vaws profile |
| `.agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py` | argparse | - | 33 | routing:1, skill-doc:3, test:1 | mechanics | vaws profile |
| `.agents/skills/ascend-profiling-collection/scripts/profile_control.py` | argparse | - | 4 | routing:1, script:1, skill-doc:3 | redundant | .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py |
| `.agents/skills/ascend-profiling-collection/scripts/run_remote_analyse.py` | argparse | - | 7 | routing:1, script:1, skill-doc:3 | redundant | .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py |
| `.agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py` | argparse | plan, record, analyze | 3 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py` | argparse | plan, record, analyze | 4 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/ascend-triton-kernel-validation/scripts/validate_triton_impl.py` | argparse | - | 1 | skill-doc:1, test:1 | mechanics | vaws lint |
| `.agents/skills/ascend-triton-operator-development/scripts/triton_development.py` | argparse | plan, finalize | 6 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/ascend-triton-workflow/scripts/triton_workflow.py` | argparse | plan, link, finalize | 4 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py` | argparse | list, inspect, promote, merge, reject, deprecate | 9 | other:1, routing:1, skill-doc:2, test:2 | mixed | vaws knowledge |
| `.agents/skills/machine-management/scripts/inventory.py` | argparse | summary, get, put, upsert, remove | 15 | routing:1, script:4, skill-doc:4, test:1 | redundant | .agents/skills/machine-management/scripts/machine_add.py |
| `.agents/skills/machine-management/scripts/machine_add.py` | argparse | - | 13 | routing:1, script:3, skill-doc:4 | mechanics | vaws machine |
| `.agents/skills/machine-management/scripts/machine_remove.py` | argparse | - | 1 | routing:1, script:1, skill-doc:4 | mechanics | vaws machine |
| `.agents/skills/machine-management/scripts/machine_repair.py` | argparse | - | 8 | routing:1, script:1, skill-doc:4 | mechanics | vaws machine |
| `.agents/skills/machine-management/scripts/machine_verify.py` | argparse | - | 2 | mirror:1, routing:1, script:1, skill-doc:4 | mechanics | vaws machine |
| `.agents/skills/machine-management/scripts/manage_machine.py` | argparse | probe-host, bootstrap-host-key, bootstrap-container, smoke, verify-machine, mesh-export-key, mesh-add-peer, mesh-remove-peer, clean-local-known-hosts, remove-container | 25 | routing:1, script:5, skill-doc:4 | redundant | .agents/skills/machine-management/scripts/machine_add.py |
| `.agents/skills/modelscope/scripts/download_from_modelscope.py` | argparse | - | 12 | mirror:2, routing:1, script:1, skill-doc:1 | mechanics | payload |
| `.agents/skills/modelscope/scripts/modelscope_auto.py` | argparse | ensure, status, verify, worker | 11 | mirror:1, routing:1, skill-doc:1 | mechanics | vaws model |
| `.agents/skills/modelscope/scripts/modelscope_download_status.py` | argparse | - | 3 | mirror:1, routing:1, skill-doc:1 | redundant | .agents/skills/modelscope/scripts/modelscope_auto.py |
| `.agents/skills/modelscope/scripts/verify_modelscope_sha256.py` | argparse | - | 8 | mirror:2, routing:1, script:1, skill-doc:1 | mechanics | payload |
| `.agents/skills/npu-fleet-monitor/scripts/manage_monitor.py` | argparse | ensure, status, restart, stop | 2 | docs:1, routing:3, skill-doc:1, test:1 | mechanics | vaws machine |
| `.agents/skills/remote-code-parity/scripts/gc_runtime_cache.py` | argparse | - | 7 | routing:1, skill-doc:3 | mechanics | vaws sync |
| `.agents/skills/remote-code-parity/scripts/install_consent.py` | argparse | resolve, set, batch-set, resolve-sync-mode, set-sync-mode | 7 | routing:1, script:1, skill-doc:4 | mechanics | vaws sync |
| `.agents/skills/remote-code-parity/scripts/parity_sync.py` | argparse | - | 17 | mirror:1, routing:2, script:3, skill-doc:8, test:2 | mechanics | vaws sync |
| `.agents/skills/remote-code-parity/scripts/parity_watch.py` | argparse | - | 2 | routing:1, skill-doc:3, test:1 | mechanics | vaws sync |
| `.agents/skills/remote-code-parity/scripts/remote_code_parity.py` | argparse | plan, sync | 9 | routing:1, script:3, skill-doc:3, test:2 | mechanics | payload |
| `.agents/skills/remote-code-parity/scripts/transport_benchmark.py` | argparse | - | 4 | skill-doc:2 | mechanics | harness |
| `.agents/skills/repo-init/scripts/install_gh_user.py` | bare | - | 0 | script:1 | mechanics | vaws workspace |
| `.agents/skills/repo-init/scripts/repo_init_probe.py` | argparse | - | 1 | mirror:1, routing:1, skill-doc:4 | mechanics | vaws workspace |
| `.agents/skills/repo-init/scripts/repo_init_profile.py` | argparse | plan, apply, apply-alias | 3 | routing:1, skill-doc:4 | mechanics | vaws workspace |
| `.agents/skills/repo-init/scripts/repo_topology.py` | argparse | compare-main, configure, ensure-main | 8 | routing:1, skill-doc:4 | mechanics | vaws workspace |
| `.agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py` | argparse | - | 1 | skill-doc:4 | mechanics | vaws workspace |
| `.agents/skills/session-management/scripts/npu_coordination.py` | argparse | submit, acquire, preflight, activate, heartbeat, release, cancel, status, gc, hold-add, hold-remove | 37 | mcp:1, routing:3, script:1, skill-doc:4 | mechanics | vaws session |
| `.agents/skills/session-management/scripts/session_create.py` | argparse | - | 20 | routing:2, script:2, skill-doc:12, test:3 | mechanics | vaws session |
| `.agents/skills/session-management/scripts/session_diff.py` | argparse | - | 3 | script:1, skill-doc:4 | mechanics | vaws session |
| `.agents/skills/session-management/scripts/session_gc.py` | argparse | - | 3 | routing:1, skill-doc:5, test:1 | mechanics | vaws session |
| `.agents/skills/session-management/scripts/session_group.py` | argparse | create, status, list, teardown | 7 | routing:1, skill-doc:3, test:1 | mechanics | vaws session |
| `.agents/skills/session-management/scripts/session_list.py` | argparse | - | 1 | routing:1, skill-doc:2 | mechanics | vaws session |
| `.agents/skills/session-management/scripts/session_remove.py` | argparse | - | 6 | routing:1, script:2, skill-doc:5, test:1 | mechanics | vaws session |
| `.agents/skills/session-management/scripts/session_status.py` | argparse | - | 2 | other:1, routing:1, skill-doc:1 | mechanics | vaws session |
| `.agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py` | argparse | - | 28 | skill-doc:3 | mixed | vaws bench |
| `.agents/skills/vllm-ascend-benchmark/scripts/bench_run.py` | argparse | - | 16 | routing:1, skill-doc:6 | mechanics | vaws bench |
| `.agents/skills/vllm-ascend-change-validation/scripts/change_validation.py` | argparse | plan, link, finalize | 11 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/vllm-ascend-correctness-validation/scripts/aisbench_adapter.py` | argparse | prepare, normalize | 19 | skill-doc:2, test:1 | mechanics | vaws bench |
| `.agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py` | argparse | init, compare | 13 | routing:1, skill-doc:2, test:1 | mixed | vaws bench |
| `.agents/skills/vllm-ascend-correctness-validation/scripts/remote_correctness_harness.py` | argparse | - | 2 | skill-doc:2, test:1 | mechanics | payload |
| `.agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py` | argparse | init, ingest, analyze | 3 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py` | argparse | init, record, compare, finalize | 25 | routing:1, skill-doc:3, test:1 | mixed | guidance |
| `.agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py` | argparse | plan, start, status, smoke, stop | 4 | routing:1, skill-doc:2, test:1 | mixed | vaws serve |
| `.agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py` | argparse | plan, record, normalize, analyze | 9 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py` | argparse | - | 3 | mirror:1, routing:2, skill-doc:6 | redundant | .agents/skills/machine-management/scripts/machine_verify.py |
| `.agents/skills/vllm-ascend-serving/scripts/serve_start.py` | argparse | - | 16 | mirror:1, routing:1, script:4, skill-doc:12, test:1 | mechanics | vaws serve |
| `.agents/skills/vllm-ascend-serving/scripts/serve_status.py` | argparse | - | 2 | mirror:1, routing:1, script:1, skill-doc:4 | mechanics | vaws serve |
| `.agents/skills/vllm-ascend-serving/scripts/serve_stop.py` | argparse | - | 3 | mirror:1, routing:1, script:4, skill-doc:13, test:1 | mechanics | vaws serve |
| `.remote-dev/core/managed_jobs.py` | bare-argv | - | 0 | mcp:1, test:1 | mechanics | payload |
| `.remote-dev/hooks/claude_remote_guard.py` | bare | - | 0 | client-config:1, test:1 | mechanics | hook |
| `.remote-dev/hooks/codex_remote_guard.py` | bare | - | 0 | client-config:1, test:1 | mechanics | hook |
| `.remote-dev/mcp/server.py` | bare | - | 0 | client-config:2, other:3, routing:1, script:1, test:1 | mechanics | server |
| `.remote-dev/tools/remote_apply_patch.py` | delegated | - | 19 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_artifact_manifest.py` | delegated | - | 17 | routing:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_artifact_pull.py` | delegated | - | 18 | routing:1, skill-doc:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_artifact_push.py` | delegated | - | 18 | routing:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_bash.py` | delegated | - | 20 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_context_snapshot.py` | delegated | - | 17 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_edit.py` | delegated | - | 20 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_glob.py` | delegated | - | 20 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_grep.py` | delegated | - | 23 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_job_status.py` | delegated | - | 17 | routing:1, test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_job_stop.py` | delegated | - | 18 | routing:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_job_tail.py` | delegated | - | 19 | routing:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_ls.py` | delegated | - | 19 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_monitor.py` | delegated | - | 20 | test:1 | redundant | .remote-dev/tools/remote_bash.py |
| `.remote-dev/tools/remote_multi_edit.py` | delegated | - | 18 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_probe.py` | delegated | - | 16 | routing:1, test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_read.py` | delegated | - | 20 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_write.py` | delegated | - | 21 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/sync_claude_skills.py` | argparse | - | 1 | other:2, routing:1, script:1, test:1 | mechanics | vaws lint |
| `.remote-dev/tools/validate_remote_dev_scaffold.py` | argparse | - | 14 | other:2, routing:1 | mechanics | vaws lint |
| `.trae/skills/modelscope/scripts/download_from_modelscope.py` | argparse | - | 12 | mirror:2, routing:1, script:1, skill-doc:1 | redundant | .agents/skills/modelscope/scripts/download_from_modelscope.py |
| `.trae/skills/modelscope/scripts/modelscope_auto.py` | argparse | ensure, status, verify, worker | 11 | mirror:1, routing:1, skill-doc:1 | redundant | .agents/skills/modelscope/scripts/modelscope_auto.py |
| `.trae/skills/modelscope/scripts/modelscope_download_status.py` | argparse | - | 3 | mirror:1, routing:1, skill-doc:1 | redundant | .agents/skills/modelscope/scripts/modelscope_auto.py |
| `.trae/skills/modelscope/scripts/verify_modelscope_sha256.py` | argparse | - | 8 | mirror:2, routing:1, script:1, skill-doc:1 | redundant | .agents/skills/modelscope/scripts/verify_modelscope_sha256.py |
