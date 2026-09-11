# Agent call

From the repository root:

```text
python .agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py --config validation.json --kernel kernel.py --results case-results.json
```

The config contains op_name, reference, target, cases and tolerances. The tool checks kernel source for missing launches and computation fallback, combines observed case results, and emits coverage, analysis and a manifest automatically.

Use `--help` for exact argument details. Report output directories are optional
where supported; the script creates a fresh directory under `.vaws-local/`.
Schema versions and report identifiers are generated internally. Input files
describe business cases or contain observed results, rather than task ownership.
