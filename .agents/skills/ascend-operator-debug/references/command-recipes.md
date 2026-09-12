# Agent call

From the repository root:

```text
uv run --no-project python .agents/skills/ascend-operator-debug/scripts/operator_debug.py --config operator.json --results case-results.json
```

The config contains operator identity, tolerance and cases. Result files contain observed case metrics or failures. The report computes coverage and classification; absent cases remain inconclusive.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
