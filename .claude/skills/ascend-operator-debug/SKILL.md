<!-- Generated Claude Code shim from .agents/skills/ascend-operator-debug/SKILL.md. Do not edit. -->
---
name: ascend-operator-debug
description: Reduce an Ascend model-level failure to one torch_npu, ACLNN, or custom operator call, then validate explicit dtype, shape, layout, and eager/compile/graph cases against a reference implementation. Use for operator crashes, unsupported dtype or layout errors, shape-dependent numerical mismatches, or workspace API faults. Do not use for whole-model graph localization, multi-rank failures, performance benchmarking, or profiler analysis.
---

# Ascend Operator Debug

Canonical skill source:

`.agents/skills/ascend-operator-debug/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use `.remote-dev` companion tools for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
