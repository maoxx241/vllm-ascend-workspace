# Agent call

From the repository root:

```text
uv run --no-project python .agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py --cases cases.json --baseline baseline.json --candidate candidate.json
```

The remote_correctness_harness.py payload captures offline runtime observations from the managed execution. Online/AISBench results use the server execution reference through aisbench_adapter.py. The comparison derives metadata from actual outputs, emits its certificate and report, and reports missing identity as inconclusive.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
