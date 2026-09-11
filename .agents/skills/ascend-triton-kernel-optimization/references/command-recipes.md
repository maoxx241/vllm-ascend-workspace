# Agent call

From the repository root:

```text
python .agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py --config optimization.json --results round-results.json
```

The config contains op_name, kernel and its validation evidence, target, cases, baseline measurements and objective. Round results carry candidate measurements and validation. The report computes KEEP/DISCARD and verifies kernel lineage and case coverage.

Use `--help` for exact argument details. Report output directories are optional
where supported; the script creates a fresh directory under `.vaws-local/`.
Schema versions and report identifiers are generated internally. Input files
describe business cases or contain observed results, rather than task ownership.
