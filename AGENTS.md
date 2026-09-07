# Repository instructions

Local `vllm` + `vllm-ascend` development scaffold. `vllm/` and `vllm-ascend/` are Git submodules.

This repository provides a remote development substrate first, then
vLLM-Ascend skills on top.

## Remote development model

Use native client tools for local files and shell work. Use `.remote-dev`
companion tools for ordinary remote read/edit/bash/search/patch/job/artifact
work; endpoint defaults and tool mappings live in `.remote-dev/README.md`.

Choose the runtime interface that matches the task:

- Ordinary remote endpoint work uses `host + port`; it does not require a VAWS
  session. Direct endpoints default to `root=/`; narrow it when isolation is
  required.
- Legacy domain wrappers for serving, benchmark, and profiling require a
  `session-management` session. Inside its worktree they resolve the cwd
  binding; outside it pass `--session-id` or `--session-file`.
- Pool work uses `vaws_session/vaws_run/vaws_execution/vaws_finish` and the
  [coordinator contract](.agents/coordinator/README.md). Task, binding, and
  execution ids are not legacy session ids; do not create duplicate NPU leases.

## Skills

Repo-local skills live under `.agents/skills/`. Each has its own `SKILL.md` with usage, entry points, and routing rules — read that before invoking.

| Skill | Purpose |
|-------|---------|
| `repo-init` | Initialize workspace: `gh`, GitHub auth, submodules, fork topology |
| `machine-management` | Add / verify / repair / remove a remote NPU machine |
| `npu-fleet-monitor` | Locate or bootstrap the standalone vaws-top worktree and query fleet status |
| `session-management` | Create / inspect / remove / group isolated agent sessions (local worktree + remote container + leases) |
| `remote-toolbox` | Compatibility backend for managed VAWS target/probe/exec/job/sync/service/artifact/cleanup tools |
| `remote-code-parity` | Sync local working tree to remote container before execution |
| `modelscope` | Download / resume / status-check / SHA256-verify ModelScope model weights under explicit local directories |
| `vllm-ascend-serving` | Start / check / stop a vLLM Ascend service on a remote container |
| `vllm-ascend-benchmark` | Run `vllm bench serve` benchmarks (single-run or multi-run with warmup) |
| `ascend-memory-profiling` | Profile HBM memory usage on Ascend NPU for vLLM serving scenarios |
| `ascend-profiling-collection` | Collect one Ascend torch-profiler case end-to-end (start service, bracket workload with `/start_profile` + `/stop_profile`, run `analyse()`, verify outputs, write manifest) |
| `ascend-profiling-analysis` | Analyze collected Ascend torch-profiler roots/manifests and generate reports |
| `curate-workspace-knowledge` | Explicitly review, deduplicate, promote, merge, reject, or deprecate verified knowledge candidates |
| `vllm-ascend-graph-debug` | Diagnose Ascend graph compile, capture, replay, and graph/eager correctness divergence |
| `vllm-ascend-correctness-validation` | Plan and compare baseline/candidate, eager/graph, offline/online, and task-metric correctness evidence |
| `vllm-ascend-change-validation` | Map code diffs to required validation evidence and aggregate downstream runs into PR reports |
| `vllm-ascend-performance-regression` | Control alternating baseline/candidate experiments and assess performance regressions |
| `vllm-ascend-distributed-debug` | Diagnose rank topology, process-group, endpoint, collective, and distributed hang failures |
| `ascend-operator-debug` | Reduce a model symptom to one Ascend operator call and validate an explicit input/mode matrix |
| `ascend-triton-operator-development` | Convert a PyTorch or GPU Triton contract into a first correct Ascend Triton candidate |
| `ascend-triton-kernel-validation` | Detect fallback and validate an Ascend Triton kernel over an explicit correctness matrix |
| `ascend-triton-kernel-optimization` | Run profiler-driven, correctness-gated Ascend Triton optimization experiments |
| `ascend-triton-workflow` | Orchestrate Ascend Triton development, validation, optimization, and evidence linking |
| `vllm-ascend-pd-serving` | Orchestrate grouped prefill/decode services, connector configuration, rollback, and smoke tests |

None of these are gates for normal local coding, docs work, or unrelated Git tasks.
For remote endpoint work, prefer `.remote-dev` tools first and use these skills
for domain workflows.

## Repo-wide rules

- Native-attached VAWS tasks use one local task identity with many native
  attachments. New native sessions create new tasks; resume keeps the original
  task. Only explicit parent/user association joins another task; never infer
  task identity from cwd or recent chat history.
- The optional shared runtime pool is documented in `.agents/coordinator/README.md`.
  Bind actual business worktrees. All clients of a pool must use the same manager.
  Stage edits during runs; materialize and refresh native artifacts only
  after its executions are released. Model services restart for changed code.
- Never write secrets, passwords, or tokens into tracked files.
- Keep VAWS runtime state under `.vaws-local/` and remote-dev endpoint/tool
  state under `.remote-dev/state/`. Both are untracked.
- Keep `.gitmodules` on community upstream URLs.
- Skill wrappers: progress on `stderr`, final JSON on `stdout`.
- Execution skills must use Run Manifest v1 from `.agents/lib/vaws_run_manifest.py` for new cross-workflow runs and keep manifests under untracked `.vaws-local/`.
- Read fast-changing compatibility, capability, validation, and failure-signature facts from `.agents/knowledge/`; treat missing facts as unknown rather than supported.
- On a concrete practical failure, query compact formal matches with `.agents/scripts/knowledge_query.py` before repeating diagnosis. After a novel fix has a confirmed cause and verification evidence, capture a candidate with `.agents/scripts/knowledge_capture.py`.
- Invoke `curate-workspace-knowledge` only for explicit knowledge review or promotion; normal workflows use the shared capture and query scripts directly.
- Before reporting a blocker caused by a concrete runtime failure, query `.agents/knowledge/` with `.agents/scripts/knowledge_query.py` using its observed signature. State when no verified match exists. Missing user preferences are not failure signatures.
- Machine registration and `session_create.py` base-machine selection use
  `--machine`. Legacy compatibility exceptions are `parity_sync.py`,
  `npu_coordination.py`, and `serve_probe_npus.py`; check each command's help.
- Run Ascend execution, model inference, and `torch`/`torch_npu`-dependent tests
  in a ready remote container. Portable scaffold checks (catalog, CLI, state,
  and mocked control-plane tests) can run locally without NPU dependencies.

## Working agreement

Follow the user's requested outcome and existing authorization throughout the
workflow. A question or audit asks for an assessment; a request to fix or
implement asks for the work itself. Finish the authorized work and applicable
checks. If one part is blocked, continue independent parts and report what is
missing; do not silently reduce scope.

- Reuse choices already supplied in the request, conversation, or persisted
  state. Ask only for missing decisions that materially affect the work or for
  actions beyond the authorized scope. A skill's decision step does not require
  asking again for the same authorized target and operation.
- Preserve dirty or conflicted checkouts; use an isolated worktree when needed.
  Before cleanup or restart, establish the exact process/container/session
  ownership. Permission to inspect or monitor does not authorize termination.
- Read the selected skill before invoking it; load supporting references for
  the active phase. Local docs and Git work do not require remote setup or a
  read-through of every skill. Read submodule instructions when working there.
- Preserve logs and the baseline before changing a failed runtime. Use bounded
  instrumentation or an isolated candidate to test a hypothesis; distinguish a
  hypothesis from a verified cause and fix. Do not revert the user's edits.
- Batch tool calls only when their inputs and authorization are already known
  and independent. Skill routing precedes its dependent operations; parity,
  service start, and benchmark remain ordered. Prefer targeted edits.
- Run checks proportionate to changed behavior. Add a regression test when it
  protects meaningful behavior; prose-only changes need link/catalog review,
  not invented runtime tests. Keep disposable probes in `/tmp` or untracked
  `.vaws-local/`. Avoid unrelated fixes and repeated suites without new evidence.
- Check the checked-out source and runtime for changing flags, model structures,
  operators, and image tags. Treat knowledge matches as leads to verify.
- Give concise progress updates during long work. Report the outcome, relevant
  paths/metrics, verification performed, and unresolved limits in the final
  response; tool JSON alone is not a user-facing result. Service readiness,
  correctness, performance, and acceptance are separate evidence levels.

## Maintenance

When changing a skill, update the whole package together: `SKILL.md`, `scripts/`, `references/`, `agents/`, and other supporting files as applicable. When the change affects shared state, also update `.agents/scripts/workspace_profile.py`, `.agents/lib/vaws_local_state.py`, `.agents/lib/vaws_session_id.py`, `.agents/lib/vaws_session_state.py`, and `.agents/lib/vaws_remote_toolbox.py` as applicable.
