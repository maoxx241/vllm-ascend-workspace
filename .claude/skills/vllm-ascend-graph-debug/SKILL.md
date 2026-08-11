<!-- Generated Claude Code shim from .agents/skills/vllm-ascend-graph-debug/SKILL.md. Do not edit. -->
---
name: vllm-ascend-graph-debug
description: Diagnose vLLM Ascend cudagraph and ACL Graph compile, capture, replay, hang, and graph-versus-eager correctness problems. Use when eager passes but graph mode fails, hangs, or diverges, or when graph/eager intermediate tensors must be aligned. Do not use to plan a correctness matrix, after the failure is reduced to one operator, when eager itself fails, or for performance profiling or HBM attribution.
---

# NPU Graph Debug

Canonical skill source:

`.agents/skills/vllm-ascend-graph-debug/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use `.remote-dev` companion tools for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
