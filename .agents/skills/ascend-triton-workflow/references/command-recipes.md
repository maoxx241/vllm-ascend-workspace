# Agent call

From the repository root:

```text
python .agents/skills/ascend-triton-workflow/scripts/triton_workflow.py --config operator.json --development development/manifest.json --validation validation/manifest.json --optimization optimization/manifest.json
```

The config contains op_name, source, target, cases and required_stages. One report call verifies stage scope, actual artifacts, passing cases and kernel identity. Missing or unrelated evidence cannot complete the workflow. Stage identifiers and linking are internal.

Use `--help` for exact argument details. Report output directories are optional
where supported; the script creates a fresh directory under `.vaws-local/`.
Schema versions and report identifiers are generated internally. Input files
describe business cases or contain observed results, rather than task ownership.
