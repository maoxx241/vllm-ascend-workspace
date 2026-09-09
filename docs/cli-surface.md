# CLI surface: current inventory and historical proposal

Status: current

This document is the inventory and guidance record for the scaffold CLI
surface. It does **not** implement the historical thirteen-command dispatcher,
delete or rename business CLIs, or change domain validators.

Regenerate the current numbers and the current table with:

```bash
python3 .agents/scripts/cli_surface_inventory.py --format summary   # counts only
python3 .agents/scripts/cli_surface_inventory.py --format markdown  # the current table
python3 .agents/scripts/cli_surface_inventory.py                    # full JSON
```

The unit test `.agents/tests/test_cli_surface_inventory.py` checks overlay
coherence against emitted rows and the delimited **current** table. The dated
132-entry snapshot is a historical fixture, not a CI invariant of ordinary new
entry points.

## 1. Inspected context

| Fact | Value |
|---|---|
| Scaffold source | `vllm-ascend-workspace/vllm-ascend-workspace` |
| Accepted public scaffold main | `84f7e865a4e698d244c1cbe6cab6a2c5cca21067` |
| That main's tree | `d8d5b42bfb07a97895b46281a664c7bec57e746d` |
| Original #85 | `b6e8559bc6e76743ffd08a383072c8b041a30e12` |
| Ordinary-merge preview tree before these three files | `626e553438e5b24451c8735682b0c0ce6e76d89c` |
| Measurement | AST `__main__` / `__main__.py` discovery of tracked (and untracked-unignored) Python outside `vllm/` and `vllm-ascend/`; overlay and owner selectors from `.agents/policy/cli-surface-inventory.json`; `pyproject.toml` / `uv.lock`; no import, fetch, `--help`, NPU or SSH |

The census is the AST of the inspected files plus those committed package owners. The
120-entry overlay and four owner-selector records live in
`.agents/policy/cli-surface-inventory.json` (metadata only; not executable).
Re-run the generator after this overlay changes. Do not paste a future commit
SHA into this file as if it were an input.

## 2. Current headline

These totals are derived from the current overlay and the emitted rows. They
are **not** the original 132-entry snapshot and **not** the unimplemented
13-command target.

| Measure | Value |
|---|---|
| Entry points (definition in §3) | **119** |
| Supported agent-facing launchers | 69 |
| Compatibility wrappers | 17 |
| Internal / diagnostic CLIs | 13 |
| Generated projections | 4 |
| Hooks | 3 |
| Remote payloads | 9 |
| Test / maturation harnesses | 4 |
| Responsibility | mechanics 103 · mixed 16 · judgment 0 |
| Files importing `argparse` (non-test) | 99 |
| Skills that ship at least one entry point | 24 |
| Parser styles | argparse 97 · delegated 18 · bare 3 · bare-argv 1 |
| Historical snapshot (original #85) | 132 entries; mechanics 81 · judgment 8 · mixed 8 · redundant 35 |
| Historical proposed surface | **13 nouns**, 75 verbs (unimplemented) |

The 119 roles are non-overlapping: every discovered entry has exactly one
support role, and the seven role counts sum to 119. Support role is not
inferred from a future `vaws <noun>` label, from a `__main__` guard alone, or
from the absence of a basename mention.

## 3. Method

An **entry point** is a Python file that

- is tracked (or untracked and not ignored) outside `vllm/`, `vllm-ascend/`,
  `.git/`, `.vaws-local/`, `.vaws-local/remote-dev-state/`;
- is not a test (`tests/` directories, `test_*.py`, `selftest_*.py`,
  `conftest.py`) and not `__init__.py`;
- has an `if __name__ == "__main__":` guard or is a `__main__.py`.

For each one the tool extracts, with `ast` and without importing the code:
parser style (`argparse` inline, `delegated` to a library function, `bare` /
`bare-argv`), verbs, option strings (including helper-added ones such as
`add_target_args(parser)` and the `if tool == "bash":` branches of a local
dispatcher), the first docstring line, and every file that mentions the
script's basename. Mentions are classified as routing document, skill
document, script, hook, MCP/coordinator code, generated mirror, client
config, **policy**, **source-map** (`pyproject.toml` / `uv.lock`), or test.
When two entry points share a basename, a mention is attributed by the
directory attached to the basename; a bare basename is attributed to both and
marked as a collision.

Reference scanning is textual evidence, not proof of runtime dependence or of
safety to delete. Policy and source-map mentions are kept distinct from
executable callers. Inventory artifacts (`cli_surface_inventory.py`, the JSON
catalog, and this document) are excluded from the reference scan so the
catalog's own rows do not manufacture a mention of every measured entry. That
is an inventory reference-count distinction, not an exemption from
`repo_boundary_check.py`. The catalog remains part of the strict tracked leak
scan. The discovery miniature used by the unit test lives in
`.agents/tests/fixtures/cli-surface-inventory.json`.

The collector records the first explicit option spelling and a conservative
local parser structure. It does not interpret dynamic `tool NAME` values or
verbs dispatched before `argparse` subparsers. Known limits for current
launchers:

- `.agents/scripts/vaws.py` argparse verbs are `status`, `env`,
  `hook`, `task-server`. `attach` / `session` / `run` / `execution` /
  `finish` are delegated to the coordinator CLI outside those subparsers.
- The installed `remote-dev` console script takes hyphen tool names
  (`bash`, `probe`, `multi-edit`); this inventory does not fabricate
  provider `remote_*` verbs or options from an uninspected checkout.

## 4. Current support roles

| Role | Meaning | Current count |
|---|---|---|
| `supported` | agent-facing launcher or domain command that currently owns the mechanic | 69 |
| `compatibility` | still-present managed toolbox or legacy `--machine` wrapper whose semantics differ from the extracted provider | 17 |
| `internal` | library or pipeline stage that grew a diagnostic `__main__` | 13 |
| `generated` | Trae ModelScope projection produced from the canonical package | 4 |
| `hook` | client or git lifecycle adapter | 3 |
| `payload` | executable spawned on the container or by another command | 9 |
| `harness` | maturation / golden / stress tooling | 4 |

Responsibility (`mechanics` / `judgment` / `mixed`) is independent of support
role. A mixed script can be currently supported. A compatibility wrapper is
still a local entry point; it is not a missing file.

The **Proposed** column in the current table is the unimplemented original #85
option, when one was recorded. It is not the current owner and not a routing
instruction.

## 5. Deterministic mechanisms are not "judgment because they skip SSH"

Absence of SSH or hardware calls does not make a command pure judgment.
Shared schema validation, numerical comparison, execution identity,
comparability certificates, schedule coverage, child-parent / run-type /
artifact linkage, truthful verdict transitions and atomic manifests remain
deterministic mechanisms. Case selection, hypotheses, threshold rationale and
interpretation remain agent/user decisions. Do not replace a closed validator
with a hand-written `passed` JSON and treat that as measured evidence.

These planner/analyze commands stay. They are **mixed** and **supported**:

| Script | Mechanics retained | Judgment retained |
|---|---|---|
| `change_validation.py` | `link` / `finalize` child manifests, run-type coverage, artifacts | `plan` regex mapping of required evidence |
| `performance_regression.py` | non-finite/order/identity rejection; per-measurement observations; shared certificate consume | metric thresholds and whether a delta is acceptable |
| `distributed_debug.py` | ingest/analyze finding rules over normalized events | which topology facts to collect; `completed-without-mismatch` is not a two-state certificate |
| `operator_debug.py` | record/analyze schema and status counts | case-matrix selection |
| `triton_development.py` | finalize candidate hash / parent / case coverage | plan |
| `triton_validation.py` | record/analyze around `validate_triton_impl` | plan |
| `triton_optimization.py` | record/analyze measurements and artifacts | weighted-improvement thresholds |
| `triton_workflow.py` | link/finalize reject unplanned stages, wrong parent/run type, nonterminal children | plan |
| `graph_debug_case.py` | `compare` JSONL numeric diff with snapshot-sidecar identity | init/record/finalize hypothesis ledger |

Certificate issue/consume/identity merging lives in
`.agents/lib/vaws_comparability.py`. Run Manifest validation, lifecycle and
atomic persistence live in `vaws_coordinator.run_manifest`. Correctness
execution blocks, graph snapshot sidecars, per-measurement performance
observations and child-run evidence adapters remain because their evidence
semantics differ. A few duplicated numeric/local-artifact helper lines do not
justify a new framework in this inventory turn.

## 6. External owners

Provider-owned implementations that moved out of this tree are not missing
local commands. The scaffold counts its own launchers once. Provider CLI/MCP
paths must not inflate the local entry-point count. Provider source was not
fetched, imported or executed for this census; `source_availability` is
`uninspected`. `uv.lock` is the known boundary, not the provider's later
main. Do not copy lock commits into this table.

| Owner | Package | Scaffold launcher | Consumed surface |
|---|---|---|---|
| `vllm-ascend-workspace/remote-dev` | `vaws-remote-dev` | `.venv/bin/python -m remote_dev.mcp.server` | MCP server, hyphen CLI tools, client hooks, resolver/result contract |
| `vllm-ascend-workspace/vaws-coordinator` | `vaws-coordinator` | `.agents/scripts/vaws.py` | task-server, `python -m vaws_coordinator.vaws`, native-session hook |
| `vllm-ascend-workspace/vaws-top` | `vaws-top` (uvx only) | `.agents/skills/npu-fleet-monitor/scripts/manage_monitor.py` | fleet dashboard service; not an import |
| `vllm-ascend-workspace/vaws-knowledge` | `vaws-knowledge` | none (engine + kit) | package engine; corpus is data and is not SHA-locked |

The shared knowledge layer is the corpus inside the installed
`vaws-knowledge` package. There is no local cache import command.

Deleted in-tree remote-dev and coordinator paths are not
resurrected in the current census. A compatibility wrapper whose current
target is itself is not a `local-target-missing` finding.

## 7. Accepted C1 / C2 / C3 state

This section describes the accepted tree. It is not a sequenced rewrite of
business CLIs.

**C1 (direct read/grep/glob and ModelScope projection).** Direct remote
read/grep/glob reaches the extracted remote-dev owner through
`remote-dev read` / `grep` / `glob` (or MCP `remote_*`)
and the scaffold resolver/result adapter
(`.agents/lib/vaws_remote_dev.py`, `.agents/lib/vaws_remote_dev_plugin.py`).
MCP `remote_*` names are unchanged. Managed toolbox wrappers
(`.agents/scripts/remote_exec.py` and the job/artifact family) remain
compatibility: they still own managed-session SSH and a distinct job/artifact
store. That is not a claim that every transport or job id has been unified.

The six-file Trae ModelScope package is generated from
`.agents/skills/modelscope` by `.agents/scripts/sync_claude_skills.py`
(`--check` detects drift). Canonical scripts remain the editable source.

**C2 (comparability and manifests).** See §5. Planner/analyze commands and
their rejection tests stay. Exploratory `bench_compare.py` delta tables are
not performance-acceptance certificates.

**C3 (parity, long-running identity, in-flight jobs).** Parity still
path-spawns `parity_sync.py` / `remote_code_parity.py`. Toolbox and extracted
remote-dev job ids remain incompatible. This inventory does not launch a
hardware matrix or promise that unification. Accepted glob / Python 3.9
source behavior and the deliberate-interruption mux mitigation are not
universal cancellation isolation and are not a new hardware replay. Open
remote-dev issues stay open unless an independent owner closed them.

The coordinator `vaws.py` namespace, provider public interfaces and the
versioned result envelope (`docs/agent-feedback-contract.md`,
`.agents/lib/vaws_result_envelope.py`,
`.agents/schemas/result-envelope-v1.schema.json`) stay authoritative. This
document does not turn `vaws.py` into a global dispatcher, expose provider
private transport internals, collapse every target into a new `--target`
flag, or silently replace that envelope.

## 8. Historical / proposed thirteen-command design

At original #85 (`b6e8559bc6e76743ffd08a383072c8b041a30e12`) the design
document proposed collapsing 114 agent-facing entry points into **13 nouns
and 75 verbs** (`vaws remote`, `machine`, `session`, `sync`, `serve`,
`bench`, `profile`, `model`, `workspace`, `manifest`, `knowledge`, `lint`,
`task`), deleting eight ledger scripts, dropping `__main__` from library
CLIs, and extending `remote-dev.result.v1` with unversioned `layer` /
`command` fields.

That proposal is **unimplemented historical evidence**. It is not a current
target count, CI invariant, or already-shipped command list. Present-tense
routing and deletion directions from that draft are withdrawn:

- do not delete the planner/analyze scripts or their tests;
- do not make `.agents/scripts/vaws.py` a global noun dispatcher;
- do not point current replacements at deleted remote-dev tool scripts;
- do not treat basename silence as authorization to remove a CLI.

## 9. Historical snapshot (original #85, 132 entries)

Dated census at `b6e8559bc6e76743ffd08a383072c8b041a30e12`. Classification
then was mechanics 81 · judgment 8 · mixed 8 · redundant 35. The table is
frozen as historical evidence. Tests must not treat a row here as proof of
current coverage.

<!-- historical-cli-surface-table -->

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

<!-- /historical-cli-surface-table -->

## 10. Current inventory table

Generated by `python3 .agents/scripts/cli_surface_inventory.py --format markdown`
after the overlay in `.agents/scripts/cli_surface_inventory.py`. "Current
target" is the owner on this tree (this path, another local entry, or a
committed external contract). "Proposed" is the unimplemented original #85
option.

<!-- current-cli-surface-table -->
| Entry point | Style | Verbs | Options | Refs | Responsibility | Support role | Current target | Proposed |
|---|---|---|---|---|---|---|---|---|
| `.agents/hooks/knowledge_session_end.py` | bare | - | 0 | client-config:1, routing:1, test:1 | mechanics | hook | .agents/hooks/knowledge_session_end.py | - |
| `.agents/hooks/tracked_leak_precommit.py` | argparse | - | 8 | docs:1, policy:1, test:1 | mechanics | hook | .agents/hooks/tracked_leak_precommit.py | - |
| `.agents/hooks/vaws_session.py` | argparse | - | 4 | docs:1, other:1, policy:1, script:1, test:2 | mechanics | hook | .agents/hooks/vaws_session.py | - |
| `.agents/scripts/cli_surface_inventory.py` | argparse | - | 3 | policy:1, test:2 | mechanics | supported | .agents/scripts/cli_surface_inventory.py | vaws lint |
| `.agents/scripts/envelope_lint.py` | argparse | scan, check, run | 5 | docs:2, test:2 | mechanics | supported | .agents/scripts/envelope_lint.py | vaws lint |
| `.agents/scripts/knowledge_capture.py` | argparse | - | 9 | docs:1, hook:1, routing:2, skill-doc:5, test:4 | mixed | supported | .agents/scripts/knowledge_capture.py | vaws knowledge |
| `.agents/scripts/knowledge_export.py` | argparse | - | 7 | docs:1, routing:2, script:1, skill-doc:3, test:1 | mechanics | supported | .agents/scripts/knowledge_export.py | vaws knowledge |
| `.agents/scripts/knowledge_query.py` | argparse | - | 9 | docs:1, routing:2, skill-doc:1, test:2 | mechanics | supported | .agents/scripts/knowledge_query.py | vaws knowledge |
| `.agents/scripts/knowledge_validate.py` | argparse | - | 1 | docs:1, other:1, routing:1, skill-doc:1, test:2 | mechanics | supported | .agents/scripts/knowledge_validate.py | vaws knowledge |
| `.agents/scripts/remote_artifact_manifest.py` | delegated | - | 4 | policy:1, routing:1 | mechanics | compatibility | .agents/scripts/remote_artifact_manifest.py | vaws remote |
| `.agents/scripts/remote_artifact_pull.py` | delegated | - | 5 | policy:1, routing:1, skill-doc:1 | mechanics | compatibility | .agents/scripts/remote_artifact_pull.py | vaws remote |
| `.agents/scripts/remote_artifact_push.py` | delegated | - | 5 | policy:1, routing:1 | mechanics | compatibility | .agents/scripts/remote_artifact_push.py | vaws remote |
| `.agents/scripts/remote_cleanup.py` | delegated | - | 13 | routing:1 | mechanics | supported | .agents/scripts/remote_cleanup.py | vaws session |
| `.agents/scripts/remote_exec.py` | delegated | - | 8 | routing:1, test:1 | mechanics | compatibility | .agents/scripts/remote_exec.py | vaws remote |
| `.agents/scripts/remote_job_collect.py` | delegated | - | 5 | routing:1 | mechanics | compatibility | .agents/scripts/remote_job_collect.py | vaws remote |
| `.agents/scripts/remote_job_start.py` | delegated | - | 10 | routing:1 | mechanics | compatibility | .agents/scripts/remote_job_start.py | vaws remote |
| `.agents/scripts/remote_job_status.py` | delegated | - | 4 | policy:1, routing:1 | mechanics | compatibility | .agents/scripts/remote_job_status.py | vaws remote |
| `.agents/scripts/remote_job_stop.py` | delegated | - | 5 | policy:1, routing:1 | mechanics | compatibility | .agents/scripts/remote_job_stop.py | vaws remote |
| `.agents/scripts/remote_job_tail.py` | delegated | - | 6 | policy:1, routing:1 | mechanics | compatibility | .agents/scripts/remote_job_tail.py | vaws remote |
| `.agents/scripts/remote_probe.py` | delegated | - | 4 | docs:1, policy:1, routing:1, test:1 | mechanics | compatibility | .agents/scripts/remote_probe.py | vaws remote |
| `.agents/scripts/remote_service_logs.py` | delegated | - | 4 | routing:1 | mechanics | supported | .agents/scripts/remote_service_logs.py | vaws serve |
| `.agents/scripts/remote_service_start.py` | delegated | - | 3 | routing:1 | mechanics | compatibility | .agents/skills/vllm-ascend-serving/scripts/serve_start.py | vaws serve |
| `.agents/scripts/remote_service_status.py` | delegated | - | 3 | routing:1 | mechanics | compatibility | .agents/skills/vllm-ascend-serving/scripts/serve_status.py | vaws serve |
| `.agents/scripts/remote_service_stop.py` | delegated | - | 4 | routing:1 | mechanics | compatibility | .agents/skills/vllm-ascend-serving/scripts/serve_stop.py | vaws serve |
| `.agents/scripts/remote_sync_apply.py` | delegated | - | 6 | routing:1, skill-doc:3 | mechanics | compatibility | .agents/skills/remote-code-parity/scripts/parity_sync.py | vaws sync |
| `.agents/scripts/remote_sync_plan.py` | delegated | - | 5 | routing:1, skill-doc:3 | mechanics | compatibility | .agents/skills/remote-code-parity/scripts/parity_sync.py | vaws sync |
| `.agents/scripts/remote_target_resolve.py` | delegated | - | 3 | routing:1 | mechanics | compatibility | .agents/scripts/remote_target_resolve.py | vaws remote |
| `.agents/scripts/remote_toolbox_stress.py` | argparse | - | 13 | docs:1, routing:1 | mechanics | harness | .agents/scripts/remote_toolbox_stress.py | - |
| `.agents/scripts/repo_boundary_check.py` | argparse | - | 6 | docs:2, other:1, policy:3, script:1, test:3 | mechanics | supported | .agents/scripts/repo_boundary_check.py | vaws lint |
| `.agents/scripts/run_manifest.py` | argparse | init, validate | 11 | docs:3, other:1, policy:2, routing:1, test:2 | mechanics | supported | .agents/scripts/run_manifest.py | vaws manifest |
| `.agents/scripts/skill_catalog.py` | argparse | - | 2 | other:1, test:2 | mechanics | supported | .agents/scripts/skill_catalog.py | vaws lint |
| `.agents/scripts/sync_claude_skills.py` | argparse | - | 1 | docs:1, mirror:2, other:3, policy:1, routing:1, skill-doc:1, test:2 | mechanics | supported | .agents/scripts/sync_claude_skills.py | vaws lint |
| `.agents/scripts/tracked_leak_scan.py` | argparse | - | 10 | docs:1, hook:1, other:1, policy:1, script:1, test:3 | mechanics | supported | .agents/scripts/tracked_leak_scan.py | vaws lint |
| `.agents/scripts/tracked_path_check.py` | argparse | - | 6 | docs:2, other:1, policy:1, test:2 | mechanics | supported | .agents/scripts/tracked_path_check.py | vaws lint |
| `.agents/scripts/vaws.py` | argparse | status, env, hook, task-server | 1 | docs:2, other:2, policy:1, script:1, skill-doc:1, test:3 | mechanics | supported | .agents/scripts/vaws.py | vaws task |
| `.agents/scripts/vaws_client_setup.py` | argparse | - | 5 | docs:4, other:2, policy:1, skill-doc:2, test:2 | mechanics | supported | .agents/scripts/vaws_client_setup.py | vaws workspace |
| `.agents/scripts/vaws_deps.py` | argparse | status, doctor, sync | 0 | docs:4, policy:1, routing:3, script:1, skill-doc:4, test:2 | mechanics | supported | .agents/scripts/vaws_deps.py | vaws workspace |
| `.agents/scripts/workspace_identity.py` | argparse | summary, ensure, validate-alias, set-alias, decline-alias | 1 | docs:1, routing:1, skill-doc:5 | mechanics | supported | .agents/scripts/workspace_identity.py | vaws workspace |
| `.agents/scripts/workspace_profile.py` | argparse | summary, validate, ensure | 4 | routing:2, script:1, skill-doc:8 | mechanics | supported | .agents/scripts/workspace_profile.py | vaws workspace |
| `.agents/skills/ascend-memory-profiling/scripts/mem_analyze.py` | argparse | - | 1 | routing:1, skill-doc:1 | mixed | supported | .agents/skills/ascend-memory-profiling/scripts/mem_analyze.py | vaws profile |
| `.agents/skills/ascend-memory-profiling/scripts/mem_collect.py` | argparse | - | 25 | docs:1, routing:1, script:1, skill-doc:2, test:1 | mechanics | supported | .agents/skills/ascend-memory-profiling/scripts/mem_collect.py | vaws profile |
| `.agents/skills/ascend-memory-profiling/scripts/weight_inspector.py` | argparse | - | 0 | docs:1, routing:1, script:1, skill-doc:1 | mechanics | payload | .agents/skills/ascend-memory-profiling/scripts/weight_inspector.py | - |
| `.agents/skills/ascend-operator-debug/scripts/operator_debug.py` | argparse | plan, record, analyze | 3 | routing:1, skill-doc:2, test:1 | mixed | supported | .agents/skills/ascend-operator-debug/scripts/operator_debug.py | guidance |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py` | argparse | - | 14 | routing:1, script:2, skill-doc:9, test:1 | mechanics | payload | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/classify.py` | argparse | - | 1 | skill-doc:3 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/cross_rank.py` | argparse | - | 1 | skill-doc:4, test:1 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/diagnostics.py` | argparse | - | 1 | script:3, skill-doc:6, test:2 | mixed | payload | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/diagnostics.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report.py` | bare-argv | - | 0 | skill-doc:2 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report_v2/__main__.py` | bare | - | 0 | - | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/normalize.py` | argparse | - | 4 | skill-doc:2 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/report.py` | argparse | - | 6 | script:3, skill-doc:4, test:2 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/segment.py` | argparse | - | 3 | script:2, skill-doc:8, test:1 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/summarize.py` | argparse | - | 8 | skill-doc:5, test:1 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/sweep.py` | argparse | - | 13 | routing:1, script:2, skill-doc:4 | mechanics | payload | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/sweep.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/dev/golden_db_vs_csv.py` | argparse | - | 4 | skill-doc:2 | mechanics | harness | .agents/skills/ascend-profiling-analysis/scripts/dev/golden_db_vs_csv.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py` | argparse | - | 25 | routing:1, script:1, skill-doc:5, test:1 | mechanics | supported | .agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py | vaws profile |
| `.agents/skills/ascend-profiling-analysis/scripts/profile_sweep.py` | argparse | - | 16 | routing:1, script:1, skill-doc:4 | mechanics | supported | .agents/skills/ascend-profiling-analysis/scripts/profile_sweep.py | vaws profile |
| `.agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py` | argparse | - | 33 | docs:1, routing:1, skill-doc:3, test:1 | mechanics | supported | .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py | vaws profile |
| `.agents/skills/ascend-profiling-collection/scripts/profile_control.py` | argparse | - | 4 | docs:1, routing:1, script:1, skill-doc:3 | mechanics | internal | .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py | - |
| `.agents/skills/ascend-profiling-collection/scripts/run_remote_analyse.py` | argparse | - | 7 | docs:1, routing:1, script:1, skill-doc:3 | mechanics | internal | .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py | - |
| `.agents/skills/ascend-tensor-dump/assets/replay_op.py` | argparse | - | 9 | other:1, script:1, skill-doc:4, test:1 | mechanics | payload | .agents/skills/ascend-tensor-dump/assets/replay_op.py | - |
| `.agents/skills/ascend-tensor-dump/scripts/dump_compare.py` | argparse | scan, diff, tensors | 8 | routing:1, script:1, skill-doc:3, test:1 | mechanics | supported | .agents/skills/ascend-tensor-dump/scripts/dump_compare.py | guidance |
| `.agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py` | argparse | plan, record, analyze | 3 | routing:1, skill-doc:2, test:1 | mixed | supported | .agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py | guidance |
| `.agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py` | argparse | plan, record, analyze | 4 | routing:1, skill-doc:2, test:1 | mixed | supported | .agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py | guidance |
| `.agents/skills/ascend-triton-kernel-validation/scripts/validate_triton_impl.py` | argparse | - | 1 | skill-doc:1, test:1 | mechanics | supported | .agents/skills/ascend-triton-kernel-validation/scripts/validate_triton_impl.py | vaws lint |
| `.agents/skills/ascend-triton-operator-development/scripts/triton_development.py` | argparse | plan, finalize | 6 | routing:1, skill-doc:2, test:1 | mixed | supported | .agents/skills/ascend-triton-operator-development/scripts/triton_development.py | guidance |
| `.agents/skills/ascend-triton-workflow/scripts/triton_workflow.py` | argparse | plan, link, finalize | 4 | routing:1, skill-doc:2, test:1 | mixed | supported | .agents/skills/ascend-triton-workflow/scripts/triton_workflow.py | guidance |
| `.agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py` | argparse | list, inspect, promote, merge, reject, deprecate, resolve, verify, list-unresolved | 21 | policy:1, routing:1, skill-doc:2, test:5 | mixed | supported | .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py | vaws knowledge |
| `.agents/skills/machine-management/scripts/inventory.py` | argparse | summary, get, put, upsert, remove | 15 | docs:1, policy:1, routing:1, script:4, skill-doc:4, test:2 | mechanics | internal | .agents/skills/machine-management/scripts/machine_add.py | vaws machine |
| `.agents/skills/machine-management/scripts/machine_add.py` | argparse | - | 13 | routing:1, script:3, skill-doc:4 | mechanics | supported | .agents/skills/machine-management/scripts/machine_add.py | vaws machine |
| `.agents/skills/machine-management/scripts/machine_remove.py` | argparse | - | 1 | routing:1, script:1, skill-doc:4 | mechanics | supported | .agents/skills/machine-management/scripts/machine_remove.py | vaws machine |
| `.agents/skills/machine-management/scripts/machine_repair.py` | argparse | - | 8 | routing:1, script:1, skill-doc:4 | mechanics | supported | .agents/skills/machine-management/scripts/machine_repair.py | vaws machine |
| `.agents/skills/machine-management/scripts/machine_verify.py` | argparse | - | 2 | mirror:1, routing:1, script:1, skill-doc:4 | mechanics | supported | .agents/skills/machine-management/scripts/machine_verify.py | vaws machine |
| `.agents/skills/machine-management/scripts/manage_machine.py` | argparse | probe-host, bootstrap-host-key, bootstrap-container, smoke, verify-machine, mesh-export-key, mesh-add-peer, mesh-remove-peer, clean-local-known-hosts, remove-container | 25 | docs:1, routing:1, script:6, skill-doc:4 | mechanics | internal | .agents/skills/machine-management/scripts/machine_add.py | vaws machine |
| `.agents/skills/modelscope/scripts/download_from_modelscope.py` | argparse | - | 12 | mirror:2, routing:1, script:2, skill-doc:1, test:1 | mechanics | payload | .agents/skills/modelscope/scripts/download_from_modelscope.py | - |
| `.agents/skills/modelscope/scripts/modelscope_auto.py` | argparse | ensure, status, verify, worker | 11 | mirror:1, routing:1, script:1, skill-doc:1, test:1 | mechanics | supported | .agents/skills/modelscope/scripts/modelscope_auto.py | vaws model |
| `.agents/skills/modelscope/scripts/modelscope_download_status.py` | argparse | - | 3 | mirror:1, routing:1, script:1, skill-doc:1, test:1 | mechanics | internal | .agents/skills/modelscope/scripts/modelscope_auto.py | - |
| `.agents/skills/modelscope/scripts/verify_modelscope_sha256.py` | argparse | - | 8 | mirror:2, routing:1, script:2, skill-doc:1, test:1 | mechanics | payload | .agents/skills/modelscope/scripts/verify_modelscope_sha256.py | - |
| `.agents/skills/npu-fleet-monitor/scripts/manage_monitor.py` | argparse | deploy, start, status, restart, stop | 6 | docs:2, policy:1, routing:4, script:1, skill-doc:1, test:3 | mechanics | supported | .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py | vaws machine |
| `.agents/skills/remote-code-parity/scripts/gc_runtime_cache.py` | argparse | - | 7 | routing:1, script:1, skill-doc:3 | mechanics | supported | .agents/skills/remote-code-parity/scripts/gc_runtime_cache.py | vaws sync |
| `.agents/skills/remote-code-parity/scripts/install_consent.py` | argparse | resolve, set, batch-set, resolve-sync-mode, set-sync-mode | 7 | routing:1, skill-doc:5 | mechanics | supported | .agents/skills/remote-code-parity/scripts/install_consent.py | vaws sync |
| `.agents/skills/remote-code-parity/scripts/parity_sync.py` | argparse | - | 17 | docs:1, mirror:1, routing:2, script:4, skill-doc:10, test:2 | mechanics | supported | .agents/skills/remote-code-parity/scripts/parity_sync.py | vaws sync |
| `.agents/skills/remote-code-parity/scripts/parity_watch.py` | argparse | - | 2 | script:1, skill-doc:3, test:1 | mechanics | supported | .agents/skills/remote-code-parity/scripts/parity_watch.py | vaws sync |
| `.agents/skills/remote-code-parity/scripts/remote_code_parity.py` | bare | - | 0 | docs:2, routing:1, script:2, skill-doc:3 | mechanics | payload | .agents/skills/remote-code-parity/scripts/remote_code_parity.py | - |
| `.agents/skills/remote-code-parity/scripts/transport_benchmark.py` | argparse | - | 4 | skill-doc:2 | mechanics | harness | .agents/skills/remote-code-parity/scripts/transport_benchmark.py | - |
| `.agents/skills/repo-init/scripts/install_gh_user.py` | bare | - | 0 | script:1 | mechanics | supported | .agents/skills/repo-init/scripts/install_gh_user.py | vaws workspace |
| `.agents/skills/repo-init/scripts/repo_init_probe.py` | argparse | - | 1 | mirror:1, routing:1, skill-doc:4 | mechanics | supported | .agents/skills/repo-init/scripts/repo_init_probe.py | vaws workspace |
| `.agents/skills/repo-init/scripts/repo_init_profile.py` | argparse | plan, apply, apply-alias | 3 | routing:1, skill-doc:4 | mechanics | supported | .agents/skills/repo-init/scripts/repo_init_profile.py | vaws workspace |
| `.agents/skills/repo-init/scripts/repo_topology.py` | argparse | compare-main, configure, ensure-main | 8 | routing:1, skill-doc:4 | mechanics | supported | .agents/skills/repo-init/scripts/repo_topology.py | vaws workspace |
| `.agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py` | argparse | - | 1 | skill-doc:4 | mechanics | supported | .agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py | vaws workspace |
| `.agents/skills/session-management/scripts/npu_coordination.py` | argparse | submit, acquire, preflight, activate, heartbeat, release, cancel, status, gc, hold-add, hold-remove | 37 | docs:1, routing:2, script:1, skill-doc:4 | mechanics | supported | .agents/skills/session-management/scripts/npu_coordination.py | vaws session |
| `.agents/skills/session-management/scripts/session_create.py` | argparse | - | 20 | routing:2, script:2, skill-doc:10, test:3 | mechanics | supported | .agents/skills/session-management/scripts/session_create.py | vaws session |
| `.agents/skills/session-management/scripts/session_diff.py` | argparse | - | 3 | script:1, skill-doc:4 | mechanics | supported | .agents/skills/session-management/scripts/session_diff.py | vaws session |
| `.agents/skills/session-management/scripts/session_gc.py` | argparse | - | 3 | docs:1, routing:1, script:1, skill-doc:4, test:1 | mechanics | supported | .agents/skills/session-management/scripts/session_gc.py | vaws session |
| `.agents/skills/session-management/scripts/session_group.py` | argparse | create, status, list, teardown | 7 | routing:1, skill-doc:3, test:1 | mechanics | supported | .agents/skills/session-management/scripts/session_group.py | vaws session |
| `.agents/skills/session-management/scripts/session_list.py` | argparse | - | 1 | routing:1, skill-doc:1 | mechanics | supported | .agents/skills/session-management/scripts/session_list.py | vaws session |
| `.agents/skills/session-management/scripts/session_remove.py` | argparse | - | 6 | routing:1, script:2, skill-doc:4, test:1 | mechanics | supported | .agents/skills/session-management/scripts/session_remove.py | vaws session |
| `.agents/skills/session-management/scripts/session_status.py` | argparse | - | 2 | routing:1, skill-doc:1 | mechanics | supported | .agents/skills/session-management/scripts/session_status.py | vaws session |
| `.agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py` | argparse | - | 28 | skill-doc:3 | mixed | supported | .agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py | vaws bench |
| `.agents/skills/vllm-ascend-benchmark/scripts/bench_run.py` | argparse | - | 16 | docs:1, routing:1, skill-doc:6, test:1 | mechanics | supported | .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py | vaws bench |
| `.agents/skills/vllm-ascend-change-validation/scripts/change_validation.py` | argparse | plan, link, finalize | 11 | docs:2, routing:1, skill-doc:6, test:1 | mixed | supported | .agents/skills/vllm-ascend-change-validation/scripts/change_validation.py | guidance |
| `.agents/skills/vllm-ascend-correctness-validation/scripts/aisbench_adapter.py` | argparse | prepare, normalize | 20 | script:1, skill-doc:2, test:1 | mechanics | supported | .agents/skills/vllm-ascend-correctness-validation/scripts/aisbench_adapter.py | vaws bench |
| `.agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py` | argparse | init, compare | 15 | docs:1, routing:1, script:1, skill-doc:5, test:1 | mixed | supported | .agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py | vaws bench |
| `.agents/skills/vllm-ascend-correctness-validation/scripts/remote_correctness_harness.py` | argparse | - | 2 | script:1, skill-doc:2, test:1 | mechanics | payload | .agents/skills/vllm-ascend-correctness-validation/scripts/remote_correctness_harness.py | - |
| `.agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py` | argparse | init, ingest, analyze | 3 | docs:1, routing:1, skill-doc:2, test:1 | mixed | supported | .agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py | guidance |
| `.agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py` | argparse | init, record, compare, finalize | 31 | docs:1, routing:1, script:1, skill-doc:4, test:1 | mixed | supported | .agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py | guidance |
| `.agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py` | argparse | plan, start, status, smoke, stop | 4 | docs:1, routing:1, skill-doc:2, test:1 | mixed | supported | .agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py | vaws serve |
| `.agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py` | argparse | plan, record, normalize, analyze | 9 | docs:1, routing:1, skill-doc:3, test:1 | mixed | supported | .agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py | guidance |
| `.agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py` | argparse | - | 3 | docs:2, mirror:1, routing:2, skill-doc:6 | mechanics | compatibility | .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py | vaws machine |
| `.agents/skills/vllm-ascend-serving/scripts/serve_start.py` | argparse | - | 16 | docs:1, mirror:1, routing:1, script:4, skill-doc:12, test:2 | mechanics | supported | .agents/skills/vllm-ascend-serving/scripts/serve_start.py | vaws serve |
| `.agents/skills/vllm-ascend-serving/scripts/serve_status.py` | argparse | - | 2 | docs:1, mirror:1, routing:1, script:1, skill-doc:4 | mechanics | supported | .agents/skills/vllm-ascend-serving/scripts/serve_status.py | vaws serve |
| `.agents/skills/vllm-ascend-serving/scripts/serve_stop.py` | argparse | - | 3 | docs:1, mirror:1, routing:1, script:4, skill-doc:13, test:1 | mechanics | supported | .agents/skills/vllm-ascend-serving/scripts/serve_stop.py | vaws serve |
| `.trae/skills/modelscope/scripts/download_from_modelscope.py` | argparse | - | 12 | mirror:2, routing:1, script:2, skill-doc:1, test:1 | mechanics | generated | .agents/skills/modelscope/scripts/download_from_modelscope.py | - |
| `.trae/skills/modelscope/scripts/modelscope_auto.py` | argparse | ensure, status, verify, worker | 11 | mirror:1, routing:1, script:1, skill-doc:1, test:1 | mechanics | generated | .agents/skills/modelscope/scripts/modelscope_auto.py | - |
| `.trae/skills/modelscope/scripts/modelscope_download_status.py` | argparse | - | 3 | mirror:1, routing:1, script:1, skill-doc:1, test:1 | mechanics | generated | .agents/skills/modelscope/scripts/modelscope_download_status.py | - |
| `.trae/skills/modelscope/scripts/verify_modelscope_sha256.py` | argparse | - | 8 | mirror:2, routing:1, script:2, skill-doc:1, test:1 | mechanics | generated | .agents/skills/modelscope/scripts/verify_modelscope_sha256.py | - |
<!-- /current-cli-surface-table -->

## 11. Measurement limitations

- Parser aliases record the first explicit option spelling only.
- Main-guard recognition is limited to a direct `__name__ == "__main__"`
  comparison.
- Parser reachability is conservative (helper functions up to depth 3, constant
  loop iterables, literal `tool ==` branches).
- Dynamic provider verbs and options are not recovered from an absent checkout.
- Basename references, including policy and source-map text, are not callers.
- This inventory does not execute providers, apply client config, or run NPU or
  SSH work.
