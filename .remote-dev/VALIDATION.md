# Remote-Dev Scaffold Validation

Last updated: 2026-05-25.

## Current Evidence

- Local contract gates pass:
  - `python3 -m compileall -q .remote-dev .agents`
  - `python3 -m unittest discover -s .remote-dev/tests` -> 71 tests
  - `python3 -m unittest discover -s .agents/tests` -> 15 tests
  - `python3 .remote-dev/tools/sync_claude_skills.py --check`
  - `git diff --check -- .remote-dev .agents AGENTS.md CLAUDE.md .mcp.json .codex .claude .gitignore`
- `validate_remote_dev_scaffold.py --local-only` passes and reports:
  - 18 MCP tools
  - 18 CLI fallbacks
  - endpoint selector `anyOf` expressed by normal remote tool schemas
  - max tool-specific required fields: 3
- Direct live endpoint validation passed on `<direct-validation-endpoint>` with 3 parallel
  scratch workers. Covered probe, context snapshot, bash success/failure/timeout,
  cwd guards, read/edit/write, ls, glob, grep, apply_patch, artifact
  manifest/pull/push, background jobs, MCP job stdout resource, MCP artifact
  manifest resource, and cleanup.
- Two managed VAWS sessions were created concurrently on `<managed-validation-host>`:
  - `<validation-session-a>` with isolated worktree, container, and SSH port.
  - `<validation-session-b>` with isolated worktree, container, and SSH port.
- Both sessions passed `validate_remote_dev_scaffold.py --session-id ...` with
  2 parallel scratch workers each.
- Repo-root `.vaws-local/current-session.json` hash stayed unchanged:
  `2d6fdc38c2fae31b165177210ccbfb974863777d7b7d6273edbdcb18b9146525`.
- Both scratch sessions were removed with container, worktree, and lease cleanup;
  validation session records now show `status=removed`, and lease maps are empty.
- Subagent-driven stress validation created three additional managed sessions
  across two remote hosts, each with 10 parallel scratch workers. The initial
  run exposed a Codex-format `remote.apply_patch` virtual-state bug for patches
  that add a file and update/move it in the same payload. After the fix, a
  10-worker managed-session rerun and a 10-worker direct-endpoint rerun both
  passed.
- Remote-toolbox stress passed on both stress hosts with parallelism 8, 24
  concurrent `remote_exec` checks, 12 background jobs, and 256 artifact files
  per host. Stress sessions and leases were cleaned up afterward.
- Two NPU-leased managed sessions were created concurrently on `<npu-validation-host>`
  for heavy parallel validation:
  - `<heavy-session-a>` with isolated container SSH port and one leased NPU.
  - `<heavy-session-b>` with isolated container SSH port and one leased NPU.
- The two heavy sessions passed `remote-code-parity` in `source-only` and
  `materialize` modes from distinct local worktrees. Both runs used isolated
  workspace ids, cache lock paths, and manifest paths. Distinct worktree
  markers were materialized and verified remotely:
  - A: `remote_dev_parity_marker.txt` contained
    `<heavy-session-a>` identity and root commit `63cc52f`.
  - B: `remote_dev_parity_marker.txt` contained
    `<heavy-session-b>` identity and root commit `9185598`.
- Parallel service lifecycle passed with `/home/weights/Qwen3-0.6B`:
  - A: ready on its leased device and service port; stopping A left B ready.
  - B: ready on its leased device and service port; after A stopped, B still
    reported `alive=true`, `health=true`, and `models_ok=true`.
- Parallel benchmark passed in both sessions with a tiny random workload
  (`num_prompts=2`, `max_concurrency=1`, `input_len=8`, `output_len=8`):
  - A result was written under session-local `.vaws-local/sessions/<session-id>/benchmark/runs/`,
    status `ok`, output throughput about `2.13`.
  - B result was written under session-local `.vaws-local/sessions/<session-id>/benchmark/runs/`,
    status `ok`, output throughput about `2.25`.
  - After benchmark cleanup, both sessions reported `service_alive.ok=false`
    and `live_leases.service_ports=[]`.
- Parallel profiling collection passed in both sessions with the same tag
  `remote-dev-same-tag`, proving run directories do not collide:
  - A and B manifests were written under distinct
    `.vaws-local/ascend-profiling-collection/runs/<timestamp>_<tag>_<session>_<pid>_<uuid>/`
    directories.
  - Both manifests ended with `status=ok`, `workload_status.status=ok`,
    `rank_count=1`, `analysis_status=ok`, and verified
    `kernel_details.csv` plus `trace_view.json`.
- Final host probe on `<npu-validation-host>` after benchmark and profiling cleanup
  showed all 8 NPU devices free and no busy HBM entries.
- The two heavy sessions were removed with container, worktree, and lease
  cleanup. Post-cleanup status showed `status=removed`, both worktree paths
  absent, and central leases for `<npu-validation-host>` empty.

## Fixes Made During Validation

- CLI fallback errors now return a JSON `remote-dev.result.v1` result instead of
  leaking tracebacks.
- `remote.apply_patch` schema now requires either `patch` or `command`.
- Artifact pull blocks unsafe manifest relpaths before writing local files.
- Hook wrappers are covered by subprocess tests for permissive Claude/Codex
  allow behavior.
- Unified `remote.apply_patch` records before sha and real diffstat.
- Codex-format `remote.apply_patch` validates all ops before writing and rolls
  back best-effort if commit fails.
- Codex-format `remote.apply_patch` now lets later ops read virtual files added
  earlier in the same patch payload.
- Unified-diff `remote.apply_patch` rejects symlink and non-regular targets in
  remote preflight before `git apply`.
- Background `remote.bash` validates cwd before launching a job and preserves the
  same cwd error statuses as foreground bash.
- Direct endpoints default to full remote-path permission (`root=/`) while
  keeping `/vllm-workspace` as the default cwd; pass an explicit narrower root
  when validating path isolation.
- Read ledgers are scoped by client context/session and now act as optimistic
  concurrency checks when present; they are not required for default edit/write
  permission.
- Claude/Codex hook examples now cover permissive MCP remote-tool and raw shell
  behavior; hooks default to allow.
- Remote read, grep, job-tail, and bash text output are capped; full logs remain
  available through refs/resources.
- Claude project skills are lightweight generated shims that point back to the
  canonical `.agents/skills/<name>/SKILL.md` sources instead of full mirrors.
- The MCP server sets a process-local `REMOTE_DEV_SESSION_ID` so default read
  ledgers are isolated per server process.
- Remote toolbox explicit `--job-id` duplicates are blocked before remote process
  launch; non-ok `remote_job_start.py` statuses now exit nonzero.
- Added `validate_remote_dev_scaffold.py` as a repeatable JSON-reporting local
  and live validation entry point.
- Memory profiling and profiling collection run directories now include safe
  tags, target/session identity, pid, and a uuid suffix instead of only
  second-level timestamp plus tag.
- Benchmark results are now persisted under session-local
  `.vaws-local/sessions/<session-id>/benchmark/runs/` paths.
- `session_status.py` now reports `live_leases` from the central lease map so
  active service ports are visible even though the session creation record is
  static.

## Remaining High-Value Validation

- Full `remote-code-parity --apply-mode install` was intentionally not run in
  the two scratch sessions because it would replace image-provided editable
  packages and trigger remote rebuild/install work. `source-only` and
  `materialize` were validated against distinct session worktrees.
- `ascend-memory-profiling` was not run end-to-end because profiling collection
  already covered real profiler artifacts, and the memory-profiling collision
  risk is now covered by local run-dir regression tests.
