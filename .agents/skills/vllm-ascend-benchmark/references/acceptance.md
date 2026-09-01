# Benchmark Skill Acceptance Criteria

## Single-run (`bench_run.py` with default `--runs 1`)

- [ ] The session is resolved from `--session-id` / `--session-file`, or auto-resolved from the nearest `.vaws-local/current-session.json` worktree binding; with no binding and no id/file, the command fails fast pointing to `--session-id` or `session_create.py`.
- [ ] Service is started via `serve_start.py` (not raw SSH).
- [ ] `vllm bench serve` executes on the remote container and returns a result JSON.
- [ ] The remote `/tmp/result_bench_*.json` filename includes a session token and random suffix so concurrent same-machine sessions cannot overwrite each other's benchmark result file.
- [ ] Output JSON has `status: "ok"` with `metrics` containing at least `output_throughput`.
- [ ] The result is persisted under `.vaws-local/sessions/<session-id>/benchmark/runs/`.
- [ ] Service is stopped after benchmark completes (no residual processes).
- [ ] If service fails to start, output has `status: "failed"` with `phase: "serve_start"`.
- [ ] If service startup returns non-ready after recording a PID, `serve_stop.py --force` is called before failure is returned.
- [ ] If benchmark fails, service is still stopped (force-kill if needed).
- [ ] `serve_start.py` and `serve_stop.py` are called for the same session, and no other session's serving state is touched.
- [ ] User-provided `--serve-args` and `--bench-args` appear in the final config.
- [ ] When `--refer-nightly` is given, nightly values fill in missing args only.
- [ ] When user args AND nightly are both given, user args win.

## Multi-run (`bench_run.py` with `--runs N`)

- [ ] Service starts once and is not restarted between runs.
- [ ] All N benchmark iterations run against the same warm service instance.
- [ ] `--warmup-runs M` marks the first M runs as warmup in `per_run`.
- [ ] `aggregated` statistics exclude warmup runs.
- [ ] `aggregated` contains `count`, and per-metric `mean`, `stddev`, `values`.
- [ ] `per_run` lists all runs with `run` number and `warmup` boolean.
- [ ] If a run fails mid-sequence, service is stopped and `completed_runs` are reported.
- [ ] `--warmup-runs` >= `--runs` (or a negative value) is rejected with a parser error (exit 2); it must be less than `--runs`.

## Progress reporting

- [ ] Progress lines go to stderr as `__VAWS_BENCHMARK_PROGRESS__=<json>`.
- [ ] Final JSON goes to stdout only.
- [ ] Serving progress lines are forwarded to stderr.
- [ ] Multi-run mode reports per-run progress with run number and warmup tag.

## Multi-state comparison contract

- [ ] Regression comparisons must use identical `--serve-args`, `--bench-args`, `--extra-env`, and `--tp` across states.
- [ ] Only the code state (branch / commit / worktree) changes between runs.
- [ ] If any configuration parameter differs, the agent explicitly records the difference and labels the result as a configuration comparison.
- [ ] The native-input digest is computed after checkout and any per-state remote patch, so it describes the effective source that will run.
- [ ] A missing or invalid native-input digest fails before serving unless `--allow-stale-native` was explicitly passed; the override records `native_input_unverified: true` and a loud warning.

## Configuration priority

- [ ] User `--serve-args` overrides nightly `server_cmd`.
- [ ] User `--bench-args` overrides nightly `benchmarks.perf`.
- [ ] User `--extra-env` overrides nightly `envs`.
- [ ] `--tp` overrides nightly `--tensor-parallel-size`.
- [ ] When no user args and no nightly ref: defaults to `--num-prompts 64 --max-concurrency 16`.
