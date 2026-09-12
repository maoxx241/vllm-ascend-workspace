---
name: ascend-triton-operator-development
description: Develop a first correct Ascend Triton operator from a PyTorch reference or migrate an existing GPU Triton kernel to Ascend, including semantic audit, explicit task contracts, hardware-aware grid and tiling design, implementation, and handoff to correctness validation. Use for new kernel implementation, CUDA/GPU Triton migration, or repairing a candidate that has not yet passed correctness. Do not use for a kernel that already passes all planned cases and only needs performance tuning, for isolated torch_npu or ACLNN debugging, or for model-level graph failures.
---

# ascend-triton-operator-development

Implement a first correct Ascend Triton operator or migrate an existing GPU Triton kernel.

Resolve semantics from the reference and callers before selecting a grid or tile. Separate logical shape from physical layout and reductions. GPU launch assumptions need an Ascend-specific design; choose a simple correct candidate before tuning.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
uv run --no-project python .agents/skills/ascend-triton-operator-development/scripts/triton_development.py --config operator.json --kernel kernel.py --validation-manifest validation/manifest.json
```

The business config contains op_name, mode, source, reference, target, cases and tolerances. The report consumes the actual kernel and validation manifest, checking kernel identity and passing case coverage. Optional --semantic-report and --sketch attach useful design artifacts.

Run ascend-triton-kernel-validation for the candidate. Continue to optimization only after the planned correctness cases pass.

The report aggregates supplied evidence. Imported numerical results and latency
measurements do not by themselves prove candidate NPU execution; this remains
unknown in the aggregate status. Keep the measured outcomes and use existing
runner or profiler evidence in the task assessment. That report limitation adds
no prerequisite for work already supported by valid execution evidence.

Read the relevant detail only when needed:

- [semantic review](references/semantic-review.md)
- [architecture and codegen](references/architecture-and-codegen.md)

- [Business input example](references/inputs.md)
