# Repository instructions

Local `vllm` + `vllm-ascend` development scaffold. `vllm/` and `vllm-ascend/` are Git submodules.

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
| `npu-fleet-monitor` | Deploy, start, inspect, restart, or stop the loopback-only NPU monitoring dashboard from its standalone worktree |
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
- Prefer `vaws_session/vaws_run/vaws_execution/vaws_finish` for task-facing pool
  work. Bind actual business worktrees and keep local development available
  without the coordinator. Do not pass new task/binding/job ids to legacy
  session commands or create duplicate local NPU leases for pool executions.
- The optional shared runtime pool is documented in `.agents/coordinator/README.md`.
  Pool bindings use its execution leases and ordinary remote-dev endpoints;
  do not create duplicate legacy local NPU leases or pass a binding id as a
  legacy session id. All clients of a pool must use the same manager.
  Stage edits during runs; materialize and refresh native artifacts only
  after its executions are released. Model services restart for changed code.
- Never write secrets, passwords, or tokens into tracked files.
- Keep VAWS runtime state under `.vaws-local/` and remote-dev endpoint/tool
  state under `.remote-dev/state/`. Both are untracked.
- Keep `.gitmodules` on community upstream URLs.
- Prefer `.remote-dev` remote companion tools or skill wrapper scripts over raw SSH / shell commands for remote operations.
- Skill wrappers: progress on `stderr`, final JSON on `stdout`.
- Execution skills must use Run Manifest v1 from `.agents/lib/vaws_run_manifest.py` for new cross-workflow runs and keep manifests under untracked `.vaws-local/`.
- Read fast-changing compatibility, capability, validation, and failure-signature facts from `.agents/knowledge/`; treat missing facts as unknown rather than supported.
- On a concrete practical failure, query compact formal matches with `.agents/scripts/knowledge_query.py` before repeating diagnosis. After a novel fix has a confirmed cause and verification evidence, capture a candidate with `.agents/scripts/knowledge_capture.py`.
- Invoke `curate-workspace-knowledge` only for explicit knowledge review or promotion; normal workflows use the shared capture and query scripts directly.
- Before reporting a blocking problem or asking the user to intervene, query `.agents/knowledge/` with `.agents/scripts/knowledge_query.py` using the observed failure signature. State explicitly when no verified match exists.
- Use the remote-dev substrate for agent-facing remote read/edit/bash/search/patch/job/artifact work. Use the remote toolbox entrypoints as the managed VAWS compatibility backend before falling back to bare SSH.
- Remote work runs inside a `session-management` session. From inside the session worktree, parity, serving, benchmark, and profiling commands auto-resolve the session from the cwd binding; pass `--session-id` only when running outside the worktree or targeting another session. Domain skill commands (serving, benchmark, profiling) are session-only; `--machine` exists only for machine registration and `session_create.py` base-machine selection. Legacy compatibility surfaces still accept `--machine`: `remote-code-parity/scripts/parity_sync.py`, `session-management/scripts/npu_coordination.py`, and `vllm-ascend-serving/scripts/serve_probe_npus.py`.
- This repo targets Huawei Ascend NPU. Local machines (Mac/PC) cannot run `torch`/`torch_npu`-dependent code. Do not attempt local test execution — go straight to the remote container.

## Working agreement

These rules apply in every client that reads this file. They adapt the
[Claude Fable 5.1 prompting guidance](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1)
to this repository; a client whose own system prompt already covers a point
loses nothing by seeing it again.

### Finish the requested work

- Assume the user is not watching in real time and cannot answer mid-task. For
  reversible actions that follow from the request, proceed without asking.
  Offer follow-ups after the work is done; do not ask permission before doing it.
- Stop for input only at: destructive or irreversible actions (removing
  machines, sessions, or containers; deleting weights or artifacts; force
  pushes), genuine scope changes, and the explicit decision gates a skill
  declares: container image policy in `machine-management`, machine username
  and unified alias in `repo-init`, first-use sync mode in
  `remote-code-parity`, repair after a real verification mismatch in
  `modelscope`. The skill's `SKILL.md` carries the exact gate wording.
- Exception: when the user is describing a problem, asking a question, or
  thinking out loud, the deliverable is your assessment. Report findings and
  stop; apply a fix only when asked.
- Before ending a turn, check your last paragraph. If it is a plan, a question,
  a list of next steps, or a promise ("I'll…", "next I would…"), do that work
  now, including retries after errors and gathering the missing information
  yourself. End the turn only when the task is complete or blocked on input
  only the user can give.
- Before a state-changing command (restarting a service, removing a container,
  rewriting a config, killing a job), confirm the evidence supports that
  specific action. A log line that matches a failure signature in
  `.agents/knowledge/` may have a different cause; the knowledge match is a
  starting point, not a verdict.

### Scope

- The request, or the plan the user approved, sets the scope. Do not narrow,
  widen, or swap it. Make routine judgment calls yourself; check in only when
  different readings would lead to materially different work.
- If part of the task is blocked, finish every other part and say exactly what
  was left out and why. Scaling the task down is the user's call.
- Pre-existing bugs, performance concerns, cleanup, or docs the task did not
  ask for are follow-ups to report in the summary, not changes to make, unless
  the requested behavior cannot work without them.
- Verify however you like, but keep scratch scripts and ad-hoc checks out of
  the tree (`/tmp` or untracked `.vaws-local/`). Commit tests only where the
  task asks for them or the touched skill already keeps tests for that kind of
  change, sized like the neighbouring test files: roughly one focused test per
  stated behavior.

### Tool use

- Before each tool round, privately list what you need next, then request
  every item that does not depend on another's result in the same response.
  Reading a skill's `SKILL.md`, probing an endpoint, and querying knowledge are
  independent; parity sync, service start, and benchmark are not.
- Prefer targeted edits over whole-file rewrites unless the file is short or
  most of it changes. This matters for long `SKILL.md` files, knowledge YAML,
  and the helper scripts.
- When you delegate (skill subagents under `agents/`, background remote jobs
  via `remote_job_*`), keep working on independent steps while they run; wait
  only when the next step depends on the result.
- Recognizing a name is not knowing its current state. vLLM flags, Ascend
  operators, model structures, and image tags change between submodule
  checkouts and releases; check the checked-out source, `.agents/knowledge/`,
  or the remote runtime before answering, even when the name is familiar.

### Reporting

- Before a long tool chain, say in a line what you are about to do; give brief
  updates as phases complete; close with a recap that stands alone: what you
  found, what you changed, what is next, and any follow-ups you did not do.
- Most clients collapse tool output. Skill wrappers print `__VAWS_PROGRESS__`
  lines on `stderr` and one JSON object on `stdout`; the user rarely sees
  either. Put the fields the user needs (state, paths, ports, metrics, error
  text) in your reply instead of re-running commands to show them.
- Say what you mean; skip metaphor and flourish. Use lists or tables when the
  content is multifaceted; otherwise write plain prose. When you reproduce a
  log line, error message, knowledge entry, or source comment verbatim, mark
  it as a quotation or code; paraphrase everything else.

## Maintenance

When changing a skill, update the whole package together: `SKILL.md`, `scripts/`, `references/`, `agents/`, and other supporting files as applicable. When the change affects shared state, also update `.agents/scripts/workspace_profile.py`, `.agents/lib/vaws_local_state.py`, `.agents/lib/vaws_session_id.py`, `.agents/lib/vaws_session_state.py`, and `.agents/lib/vaws_remote_toolbox.py` as applicable.
