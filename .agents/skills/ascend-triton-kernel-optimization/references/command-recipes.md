# Agent call

From the repository root:

```text
uv run --no-project python .agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py --config optimization.json --results round-results.json
```

The config contains op_name, kernel and its validation evidence, target, cases, baseline measurements and objective. Round results carry candidate measurements and validation. The report computes KEEP/DISCARD and verifies kernel lineage and case coverage.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
