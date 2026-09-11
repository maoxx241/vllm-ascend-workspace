# Agent call

From the repository root:

```text
python .agents/skills/ascend-triton-operator-development/scripts/triton_development.py --config operator.json --kernel kernel.py --validation-manifest validation/manifest.json
```

The business config contains op_name, mode, source, reference, target, cases and tolerances. The report consumes the actual kernel and validation manifest, checking kernel identity and passing case coverage. Optional --semantic-report and --sketch attach useful design artifacts.

Use `--help` for exact argument details. Report output directories are optional
where supported; the script creates a fresh directory under `.vaws-local/`.
Schema versions and report identifiers are generated internally. Input files
describe business cases or contain observed results, rather than task ownership.
