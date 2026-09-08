# Repository instructions

Local `vllm` + `vllm-ascend` development scaffold. `vllm/` and `vllm-ascend/` are Git submodules.

The canonical scaffold is `vllm-ascend-workspace/vllm-ascend-workspace`
(public, non-fork). Submodule URLs stay on `vllm-project/vllm` and
`vllm-project/vllm-ascend`. Personal development forks such as
`maoxx241/vllm` and `maoxx241/vllm-ascend` sit outside the organization
and are not replacement community upstreams.

This repository provides a remote development substrate first, then
vLLM-Ascend skills on top.

## Remote development model

Use native client tools for local files and local shell work.

Use remote companion tools for remote endpoints. The remote tools mirror native
tool semantics and only add endpoint fields:

| Local tool | Remote tool |
|------------|-------------|
| Read | `remote.read` |
| Edit | `remote.edit` |
| Write | `remote.write` |
| Bash | `remote.bash` |
| Glob | `remote.glob` |
| Grep | `remote.grep` |
| LS | `remote.ls` |
| Monitor | `remote.monitor` |
| apply_patch | `remote.apply_patch` |

Default endpoint fields:

- `host`
- `port`
- `user`, default `root`
- `root`, default `/`
- `cwd`, default `/vllm-workspace`

Prefer `host + port` direct endpoints for ordinary remote development.
`session_id`, `session_file`, and `machine` remain advanced compatibility
paths for managed VAWS sessions and legacy single-tenant flows.

Prefer remote companion tools for ordinary remote development. Hooks are
permissive by default, and direct endpoints default to full remote-path
permission (`root=/`). Pass a narrower `root` explicitly when a task requires
path isolation.

## Skills

Repo-local skills live under `.agents/skills/`. Each has its own `SKILL.md` with usage, entry points, and routing rules — read that before invoking.

| Skill | Purpose |
|-------|---------|
| `repo-init` | Initialize workspace: `gh`, GitHub auth, submodules, fork topology |
| `machine-management` | Add / verify / repair / remove a remote NPU machine |
| `npu-fleet-monitor` | Deploy, start, inspect, restart, or stop the loopback-only NPU monitoring dashboard from the standalone vaws-top repository |
| `session-management` | Create / inspect / remove / group isolated agent sessions (local worktree + remote container + leases) |
| `remote-toolbox` | Compatibility backend for managed VAWS target/probe/exec/job/sync/service/artifact/cleanup tools |
| `remote-code-parity` | Sync local working tree to remote container before execution |
| `modelscope` | Download / resume / status-check / SHA256-verify ModelScope model weights under explicit local directories |
| `vllm-ascend-serving` | Start / check / stop a vLLM Ascend service on a remote container |
| `vllm-ascend-benchmark` | Run `vllm bench serve` benchmarks (single-run or multi-run with warmup) |
| `ascend-memory-profiling` | Profile HBM memory usage on Ascend NPU for vLLM serving scenarios |
| `ascend-profiling-collection` | Collect one Ascend torch-profiler case end-to-end (start service, bracket workload with `/start_profile` + `/stop_profile`, run `analyse()`, verify outputs, write manifest) |
| `ascend-profiling-analysis` | Analyze collected Ascend torch-profiler roots/manifests and generate reports |
| `curate-workspace-knowledge` | Explicitly review, deduplicate, promote, merge, reject, or deprecate verified knowledge candidates; resolve unresolved v2 coordinates and gate upstream export |
| `vllm-ascend-graph-debug` | Diagnose Ascend graph compile, capture, replay, and graph/eager correctness divergence |
| `vllm-ascend-correctness-validation` | Plan and compare baseline/candidate, eager/graph, offline/online, and task-metric correctness evidence |
| `vllm-ascend-change-validation` | Map code diffs to required validation evidence and aggregate downstream runs into PR reports |
| `vllm-ascend-performance-regression` | Control alternating baseline/candidate experiments and assess performance regressions |
| `vllm-ascend-distributed-debug` | Diagnose rank topology, process-group, endpoint, collective, and distributed hang failures |
| `ascend-tensor-dump` | Capture bounded intermediate tensor dumps and locate the first stage where numbers diverge, in eager or graph mode |
| `ascend-operator-debug` | Reduce a model symptom to one Ascend operator call and validate an explicit input/mode matrix |
| `ascend-triton-operator-development` | Convert a PyTorch or GPU Triton contract into a first correct Ascend Triton candidate |
| `ascend-triton-kernel-validation` | Detect fallback and validate an Ascend Triton kernel over an explicit correctness matrix |
| `ascend-triton-kernel-optimization` | Run profiler-driven, correctness-gated Ascend Triton optimization experiments |
| `ascend-triton-workflow` | Orchestrate Ascend Triton development, validation, optimization, and evidence linking |
| `vllm-ascend-pd-serving` | Orchestrate grouped prefill/decode services, connector configuration, rollback, and smoke tests |

None of these are gates for normal local coding, docs work, or unrelated Git tasks.
For remote endpoint work, prefer remote-dev companion tools first and use these
skills for domain workflows.

## Repo-wide rules

- Native-attached VAWS tasks use one local task identity with many native
  attachments. New native sessions create new tasks; resume keeps the original
  task. Only explicit parent/user association joins another task; never infer
  task identity from cwd or recent chat history.
- Prefer `vaws_session/vaws_run/vaws_execution/vaws_finish` for task-facing pool
  work. Bind actual business worktrees and keep local development available
  without the coordinator. Do not pass new task/binding/job ids to legacy
  session commands or create duplicate local NPU leases for pool executions.
- The optional shared runtime pool lives in the installed `vaws-coordinator`
  package (`uv.lock`). See
  [docs/coordinator-consumption.md](docs/coordinator-consumption.md).
  Pool bindings use its execution leases and ordinary remote-dev endpoints;
  do not create duplicate legacy local NPU leases or pass a binding id as a
  legacy session id. All clients of a pool must use the same manager and the
  same explicit `--state-dir`. Stage edits during runs; materialize and
  refresh native artifacts only after its executions are released. Model
  services restart for changed code.
- Never write secrets, passwords, or tokens into tracked files.
- Keep VAWS runtime state under `.vaws-local/`, remote-dev state under
  `.vaws-local/remote-dev-state/`, and the local task registry under
  `.vaws-local/agent-sessions/`. All are untracked.
- Keep `.gitmodules` on community upstream URLs.
- Prefer remote-dev companion tools (`remote_*` MCP tools, launched via `.venv/bin/python -m remote_dev.mcp.server`) or skill wrapper scripts over raw SSH / shell commands for remote operations.
- Skill wrappers: progress on `stderr`, final JSON on `stdout`.
- Execution skills must use Run Manifest v1 from `.agents/lib/vaws_run_manifest.py` for new cross-workflow runs and keep manifests under untracked `.vaws-local/`.
- Read fast-changing compatibility, capability, validation, and failure-signature facts from `.agents/knowledge/`; treat missing facts as unknown rather than supported.
- Knowledge is federated across three layers: `shared` (read-only cache of `vllm-ascend-workspace/vaws-knowledge` under `.vaws-local/knowledge/shared/`), `project` (`.agents/knowledge/`, both v1 `<kind>.yaml` and v2 `<kind>.v2.yaml`), and `candidate` (unreviewed local observations). `knowledge_query.py` reads all three, names the layer per match, and reports any layer it could not consult — a degraded answer is never an authoritative "no".
- On a concrete practical failure, query compact formal matches with `.agents/scripts/knowledge_query.py` before repeating diagnosis. After a novel fix has a confirmed cause and verification evidence, capture a candidate with `.agents/scripts/knowledge_capture.py`.
- Applicability is a coordinate, not prose. Capture reads soc / cann / driver / python_abi / torch / torch_npu / vllm / vllm_ascend / model / topology / execution_mode / component from the Run Manifest, `--env`, or the candidate scope, and records anything unavailable as `unknown`. Never invent a version to fill a dimension; an unresolved dimension blocks `verified` and blocks export by design.
- Invoke `curate-workspace-knowledge` only for explicit knowledge review, promotion, coordinate resolution, or upstream export; normal workflows use the shared capture and query scripts directly.
- Propose knowledge upstream only through `.agents/scripts/knowledge_export.py`. It is the source-side redaction gate: internal addresses, user paths, hostnames, container names, internal mounts, and unresolved coordinates never leave this fork.
- Before reporting a blocking problem or asking the user to intervene, query `.agents/knowledge/` with `.agents/scripts/knowledge_query.py` using the observed failure signature. State explicitly when no verified match exists.
- Use the remote-dev substrate for agent-facing remote read/edit/bash/search/patch/job/artifact work. Use the remote toolbox entrypoints as the managed VAWS compatibility backend before falling back to bare SSH.
- Remote work runs inside a `session-management` session. From inside the session worktree, parity, serving, benchmark, and profiling commands auto-resolve the session from the cwd binding; pass `--session-id` only when running outside the worktree or targeting another session. Domain skill commands (serving, benchmark, profiling) are session-only; `--machine` exists only for machine registration and `session_create.py` base-machine selection. Legacy compatibility surfaces still accept `--machine`: `remote-code-parity/scripts/parity_sync.py`, `session-management/scripts/npu_coordination.py`, and `vllm-ascend-serving/scripts/serve_probe_npus.py`.
- The three in-process external packages (vaws-remote-dev, vaws-coordinator, vaws-knowledge) are consumed through `pyproject.toml` + `uv.lock`. vaws-top is a uvx service, not an import. Check workspace capability with `python3 .agents/scripts/vaws_deps.py doctor` before assuming remote endpoints, the task pool, fleet observation, or shared knowledge are available; a `partial` outcome names what is missing and the `uv sync` remedy. See [docs/dependency-plane.md](docs/dependency-plane.md).
- Documentation under `docs/` carries a `Status:` line. `Status: current` is a contract; `Status: dated` is evidence and is never a direction. See [docs/README.md](docs/README.md).
- This repo targets Huawei Ascend NPU. Local machines (Mac/PC) cannot run `torch`/`torch_npu`-dependent code. Do not attempt local test execution — go straight to the remote container.

## Maintenance

When changing a skill, update the whole package together: `SKILL.md`, `scripts/`, `references/`, `agents/`, and other supporting files as applicable. When the change affects shared state, also update `.agents/scripts/workspace_profile.py`, `.agents/lib/vaws_local_state.py`, `.agents/lib/vaws_session_id.py`, `.agents/lib/vaws_session_state.py`, `.agents/lib/vaws_remote_toolbox.py`, and `.agents/lib/vaws_coordinator_launch.py` as applicable.
