<!-- Generated Claude Code shim from .agents/skills/ascend-triton-kernel-optimization/SKILL.md. Do not edit. -->
---
name: ascend-triton-kernel-optimization
description: Profile and iteratively optimize a correctness-passed Ascend Triton kernel with explicit NPU baselines, per-shape measurements, UB live-set and physical-core reasoning, MTE/Vector/Scalar bottleneck attribution, one-hypothesis rounds, noise-aware KEEP/DISCARD decisions, and Run Manifest evidence. Use for single-kernel latency or throughput improvement after all planned correctness cases pass. Do not use to create or migrate the first correct kernel, bypass failed validation, assess whole-model serving regressions, attribute model HBM, or diagnose a non-Triton operator.
---

# Ascend Triton Kernel Optimization

Canonical skill source:

`.agents/skills/ascend-triton-kernel-optimization/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use `.remote-dev` companion tools for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
