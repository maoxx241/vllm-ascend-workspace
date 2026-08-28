<!-- Generated Claude Code shim from .agents/skills/ascend-triton-operator-development/SKILL.md. Do not edit. -->
---
name: ascend-triton-operator-development
description: Develop a first correct Ascend Triton operator from a PyTorch reference or migrate an existing GPU Triton kernel to Ascend, including semantic audit, explicit task contracts, hardware-aware grid and tiling design, implementation, and handoff to correctness validation. Use for new kernel implementation, CUDA/GPU Triton migration, or repairing a candidate that has not yet passed correctness. Do not use for a kernel that already passes all planned cases and only needs performance tuning, for isolated torch_npu or ACLNN debugging, or for model-level graph failures.
---

# Ascend Triton Operator Development

Canonical skill source:

`.agents/skills/ascend-triton-operator-development/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use `.remote-dev` companion tools for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
