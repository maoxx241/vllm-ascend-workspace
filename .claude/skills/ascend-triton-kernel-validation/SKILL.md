<!-- Generated Claude Code shim from .agents/skills/ascend-triton-kernel-validation/SKILL.md. Do not edit. -->
---
name: ascend-triton-kernel-validation
description: Validate one Ascend Triton kernel against a trusted reference across an explicit shape, dtype, layout, stride, scalar-option, and execution-mode case matrix, with static detection of missing kernel launches and PyTorch computation fallback plus Run Manifest evidence. Use before any performance claim, after migration or implementation changes, or for shape-dependent compile/runtime/numerical failures in a Triton candidate. Do not use to generate the kernel, optimize an already-correct kernel, diagnose a non-Triton torch_npu or ACLNN call, or localize a whole-model graph failure.
---

# Ascend Triton Kernel Validation

Canonical skill source:

`.agents/skills/ascend-triton-kernel-validation/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use `.remote-dev` companion tools for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
