---
name: ascend-triton-kernel-validation
description: Validate one Ascend Triton kernel against a trusted reference across an explicit shape, dtype, layout, stride, scalar-option, and execution-mode case matrix, using actual candidate execution evidence, numerical comparisons and optional source lint. Use before any performance claim, after migration or implementation changes, or for shape-dependent compile/runtime/numerical failures in a Triton candidate. Do not use to generate the kernel, optimize an already-correct kernel, diagnose a non-Triton torch_npu or ACLNN call, or localize a whole-model graph failure.
---

# ascend-triton-kernel-validation

Validate one Ascend Triton kernel against its reference over the cases needed by its consumers.

Select shapes, dtype, layout, strides, scalar options and execution modes from the operator contract and affected callers. Include boundary and non-contiguous cases where semantics require them. Numerical agreement must come from the launched candidate kernel.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
uv run --no-project python .agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py --config validation.json --kernel kernel.py --results case-results.json
```

The config contains op_name, reference, target, cases and tolerances. The report
combines supplied case results and emits coverage, analysis and a manifest. Its
source lint understands only the ModelNew.forward wrapper convention and is
advisory; ordinary functions and imported wrappers remain valid inputs.

The report separates `numerical_status` from `candidate_execution`. Case status
and source lint alone do not prove actual candidate NPU execution, so the tool
retains that fact as unknown and an otherwise passing report is inconclusive.
Assess existing runner or profiler evidence for the actual launch; no new
attestation form or repeat run is required when valid evidence already exists.

A failing candidate returns to ascend-triton-operator-development. A fully passing matrix can proceed to ascend-triton-kernel-optimization.

Read the relevant detail only when needed:

- [case design](references/case-design.md)

- [Business input example](references/inputs.md)
