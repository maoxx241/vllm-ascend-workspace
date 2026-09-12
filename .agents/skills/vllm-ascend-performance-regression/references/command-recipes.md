# Agent call

From the repository root:

```text
uv run --no-project python .agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py --config experiment.json
```

The business config names baseline.sources and candidate.sources (actual vllm and vllm-ascend worktrees), benchmark options, runs, warmups and thresholds. The collector passes each source selection to that run without changing task defaults, waits for its managed service, warms each launch, alternates A/B order, records runtime observations, and releases owned executions. --results accepts existing measurement files for report-only use. Missing runtime evidence yields an inconclusive report.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
