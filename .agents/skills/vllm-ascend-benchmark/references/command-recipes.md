# Agent call

From the repository root:

```text
uv run --no-project python .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py --model /models/example --runs 3 --warmup-runs 1
```

Use --execution-id to measure an existing service, or let the workflow start and clean up its own service. --serve-args and --bench-args forward business options; --preset supplies reusable defaults. The managed interpreter and actual launch observations are recorded with measurements.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
