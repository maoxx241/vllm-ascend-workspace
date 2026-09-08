<!-- Generated Claude Code shim from .agents/skills/ascend-tensor-dump/SKILL.md. Do not edit. -->
---
name: ascend-tensor-dump
description: Capture and compare bounded intermediate tensor dumps on Ascend NPU to find the first stage where numbers diverge. Use when output is wrong, non-finite, or differs between two configurations and the divergence must be localized to a stage, layer, rank, or single operator, in eager or graph mode. Do not use for performance profiling, HBM attribution, debug case bookkeeping, or before a deterministic reproduction with fixed weights and token ids exists.
---

# Ascend Tensor Dump

Canonical skill source:

`.agents/skills/ascend-tensor-dump/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use the remote-dev companion tools (`remote_*` MCP tools; CLI fallback `remote-dev <hyphen-tool> ...` or `uv run remote-dev <hyphen-tool> ...`) for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
