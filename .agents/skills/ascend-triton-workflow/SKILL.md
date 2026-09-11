---
name: ascend-triton-workflow
description: Orchestrate an end-to-end Ascend Triton operator effort across task definition, GPU-to-NPU migration or direct development, explicit correctness validation, profiler-driven optimization, and evidence aggregation with Run Manifest v1. Use when the request spans two or more lifecycle stages or asks for a complete operator delivery. Do not use for only implementing, validating, or optimizing an already-scoped kernel; route those to the owning stage Skill.
---

# ascend-triton-workflow

Carry an operator through development, validation and optimization when the request spans those stages.

Choose the stages required by the requested outcome. Reuse relevant existing evidence. Development owns implementation, validation owns the correctness matrix, and optimization owns measured tuning decisions.

## Agent entry

Run from the repository root using the platform's Python launcher. The workspace
selects its installed platform environment automatically.

```text
python .agents/skills/ascend-triton-workflow/scripts/triton_workflow.py --config operator.json --development development/manifest.json --validation validation/manifest.json --optimization optimization/manifest.json
```

The config contains op_name, source, target, cases and required_stages. One report call verifies stage scope, actual artifacts, passing cases and kernel identity. Missing or unrelated evidence cannot complete the workflow. Stage identifiers and linking are internal.

For only one stage, use its owning skill directly.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)

- [Business input example](references/inputs.md)
