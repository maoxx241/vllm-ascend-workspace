<!-- Generated Claude Code shim from .agents/skills/vllm-ascend-distributed-debug/SKILL.md. Do not edit. -->
---
name: vllm-ascend-distributed-debug
description: Diagnose vLLM Ascend multi-rank and multi-node startup, rank mapping, process-group, collective, HCCL, Ray, scheduler, connector, and distributed hang failures from structured topology and per-rank evidence. Use when a failure depends on rank count, parallel topology, nodes, collectives, or distributed endpoints. Do not use for graph-only divergence, isolated operator failures, performance benchmarking, or profiler analysis.
---

# vLLM Ascend Distributed Debug

Canonical skill source:

`.agents/skills/vllm-ascend-distributed-debug/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use the remote-dev companion tools (`remote_*` MCP tools; CLI fallback `python3 .agents/scripts/remote_dev.py tool <name> ...`) for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
