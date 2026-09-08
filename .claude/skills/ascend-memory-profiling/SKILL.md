<!-- Generated Claude Code shim from .agents/skills/ascend-memory-profiling/SKILL.md. Do not edit. -->
---
name: ascend-memory-profiling
description: Profile and attribute HBM memory usage on Ascend NPU for vLLM serving scenarios. Breaks down memory into fixed overhead, model weights, KV cache, HCCL buffers, activations, and runtime, with traceable evidence chains. Use for requests like "分析显存占用", "显存 profiling", "HBM 用了多少", "内存各部分拆分". Do not use for performance profiling (kernel timing, throughput), offline inference, or non-Ascend hardware.
---

# Ascend Memory Profiling

Canonical skill source:

`.agents/skills/ascend-memory-profiling/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use the remote-dev companion tools (`remote_*` MCP tools; CLI fallback `remote-dev <hyphen-tool> ...` or `uv run remote-dev <hyphen-tool> ...`) for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
