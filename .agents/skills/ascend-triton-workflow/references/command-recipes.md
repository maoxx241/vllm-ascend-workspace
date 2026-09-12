# Agent call

From the repository root:

```text
uv run --no-project python .agents/skills/ascend-triton-workflow/scripts/triton_workflow.py --config operator.json --development development/manifest.json --validation validation/manifest.json --optimization optimization/manifest.json
```

The config contains op_name, source, target, cases and required_stages. One report call verifies stage scope, actual artifacts, passing cases and kernel identity. Missing or unrelated evidence cannot complete the workflow. Stage identifiers and linking are internal.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
