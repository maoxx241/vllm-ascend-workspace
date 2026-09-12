# Agent call

From the repository root:

```text
uv run --no-project python .agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py --config validation.json --kernel kernel.py --results case-results.json
```

The config contains op_name, reference, target, cases and tolerances. The report combines supplied numerical results and records their coverage. `lint_triton_source.py` provides limited syntactic observations about ModelNew.forward wrappers; it cannot prove a launch or absence of computation fallback. Candidate execution remains unknown unless assessed from actual runner or profiler evidence.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
