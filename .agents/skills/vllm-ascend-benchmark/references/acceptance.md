# Benchmark Skill Acceptance Criteria

## Single-run (`bench_run.py` with default `--runs 1`)

- [ ] Service is started via `serve_start.py` (not raw SSH).
- [ ] `vllm bench serve` executes on the remote container and returns a result JSON.
- [ ] Output JSON has `status: "ok"` with `metrics` containing at least `output_throughput`.
- [ ] Service is stopped after benchmark completes (no residual processes).
- [ ] If service fails to start, output has `status: "failed"` with `phase: "serve_start"`.
- [ ] If benchmark fails, service is still stopped (force-kill if needed).
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
- [ ] `--warmup-runs` is clamped to at most `--runs - 1`.

## Progress reporting

- [ ] Progress lines go to stderr as `__VAWS_BENCHMARK_PROGRESS__=<json>`.
- [ ] Final JSON goes to stdout only.
- [ ] Serving progress lines are forwarded to stderr.
- [ ] Multi-run mode reports per-run progress with run number and warmup tag.

## Multi-state comparison contract

- [ ] Regression comparisons must use identical `--serve-args`, `--bench-args`, `--extra-env`, and `--tp` across states.
- [ ] Only the code state (branch / commit / worktree) changes between runs.
- [ ] If any configuration parameter differs, the agent explicitly records the difference and labels the result as a configuration comparison.

## Configuration priority

- [ ] User `--serve-args` overrides nightly `server_cmd`.
- [ ] User `--bench-args` overrides nightly `benchmarks.perf`.
- [ ] User `--extra-env` overrides nightly `envs`.
- [ ] `--tp` overrides nightly `--tensor-parallel-size`.
- [ ] When no user args and no nightly ref: defaults to `--num-prompts 64 --max-concurrency 16`.
