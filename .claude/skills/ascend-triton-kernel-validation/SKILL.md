---
name: "ascend-triton-kernel-validation"
description: "Validate one Ascend Triton kernel against a trusted reference across an explicit shape, dtype, layout, stride, scalar-option, and execution-mode case matrix, with static detection of missing kernel launches and PyTorch computation fallback plus Run Manifest evidence. Use before any performance claim, after migration or implementation changes, or for shape-dependent compile/runtime/numerical failures in a Triton candidate. Do not use to generate the kernel, optimize an already-correct kernel, diagnose a non-Triton torch_npu or ACLNN call, or localize a whole-model graph failure."
---

<!-- Generated from .agents/skills/ascend-triton-kernel-validation/SKILL.md. Do not edit. -->

# ascend-triton-kernel-validation

Read `.agents/skills/ascend-triton-kernel-validation/SKILL.md` and only the references needed
for the current task. The canonical skill owns the workflow.
