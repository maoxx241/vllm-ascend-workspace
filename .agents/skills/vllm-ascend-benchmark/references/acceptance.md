# Benchmark Skill Acceptance Criteria

## Single-run (`bench_run.py`)

- [ ] Service is started via `serve_start.py` (not raw SSH).
- [ ] `vllm bench serve` executes on the remote container and returns a result JSON.
- [ ] Output JSON has `status: "ok"` with `metrics` containing at least `output_throughput`.
- [ ] Service is stopped after benchmark completes (no residual processes).
- [ ] If service fails to start, output has `status: "failed"` with `phase: "serve_start"`.
- [ ] If benchmark fails, service is still stopped (force-kill if needed).
- [ ] User-provided `--serve-args` and `--bench-args` appear in the final config.
- [ ] When `--refer-nightly` is given, nightly values fill in missing args only.
- [ ] When user args AND nightly are both given, user args win.

## A/B comparison (`bench_compare.py`)

- [ ] Baseline ref is checked out and benchmarked first.
- [ ] Patched ref is checked out and benchmarked second.
- [ ] Core benchmark parameters (model, tp, bench-args) are identical for both runs.
- [ ] `--patched-extra-env` values appear only in the patched run's env.
- [ ] Output JSON contains `baseline.metrics`, `patched.metrics`, and `delta`.
- [ ] `env_diff.patched_only` lists the extra env vars applied to patched side.
- [ ] `regression` field is `true` when patched throughput < baseline * 0.97.
- [ ] Submodule is restored to its original HEAD in the `finally` block.
- [ ] If either run fails, service is stopped and error is reported.

## Progress reporting

- [ ] Progress lines go to stderr as `__VAWS_BENCHMARK_PROGRESS__=<json>`.
- [ ] Final JSON goes to stdout only.
- [ ] Serving progress lines are forwarded to stderr.

## Configuration priority

- [ ] User `--serve-args` overrides nightly `server_cmd`.
- [ ] User `--bench-args` overrides nightly `benchmarks.perf`.
- [ ] User `--extra-env` overrides nightly `envs`.
- [ ] `--tp` overrides nightly `--tensor-parallel-size`.
- [ ] When no user args and no nightly ref: defaults to `--num-prompts 64 --max-concurrency 16`.
