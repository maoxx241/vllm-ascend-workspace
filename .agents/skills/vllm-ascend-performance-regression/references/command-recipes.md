# Agent call

From the repository root:

```text
python .agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py --config experiment.json
```

The business config names baseline.sources and candidate.sources (actual vllm and vllm-ascend worktrees), benchmark options, runs, warmups and thresholds. The collector binds each source, waits for its managed service, warms each launch, alternates A/B order, records runtime observations, and releases owned executions. --results accepts existing measurement files for report-only use. Missing runtime evidence yields an inconclusive report.

Use `--help` for exact argument details. Report output directories are optional
where supported; the script creates a fresh directory under `.vaws-local/`.
Schema versions and report identifiers are generated internally. Input files
describe business cases or contain observed results, rather than task ownership.
