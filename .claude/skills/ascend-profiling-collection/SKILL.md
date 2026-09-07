<!-- Generated Claude Code shim from .agents/skills/ascend-profiling-collection/SKILL.md. Do not edit. -->
---
name: ascend-profiling-collection
description: Collect one Ascend torch-profiler case end-to-end on a workspace-managed remote NPU container. Starts a profiled vLLM service, brackets a workload with /start_profile and /stop_profile, runs analyse() (db export by default), verifies the per-rank ascend_pytorch_profiler_*.db landed, and writes a manifest the analysis skill can consume. Use for requests like "采集 profiling", "torch profiler 跑一个 case", "采一份 profile 出来", "采 profiling 给我分析". Do not use for pure performance benchmarking, HBM/memory profiling, or for analysing already-collected profiling data (that is the analysis skill's job).
---

# Ascend Profiling Collection

Canonical skill source:

`.agents/skills/ascend-profiling-collection/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use the remote-dev companion tools (`remote_*` MCP tools; CLI fallback `python3 .agents/scripts/remote_dev.py tool <name> ...`) for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
