<!-- Generated Claude Code shim from .agents/skills/ascend-profiling-analysis/SKILL.md. Do not edit. -->
---
name: ascend-profiling-analysis
description: Analyze Ascend NPU torch profiler output (kernel_details.csv / trace_view.json / op_summary / communication.json) for one or many profiling roots and produce a traceable report (rank/step/layer/operator summary, cross-rank alignment, diagnosis findings, report.md / report.xlsx / report.html with single-step inspectors, bubble tracing axes, and zoomable Chrome-tracing-style timelines). Use for requests like "分析 profiling", "解析这份 kernel_details", "看 step/layer 切分", "跨 rank 对齐", "通信慢/EP 不均/快慢卡", "生成 profiling 报告". Do not use for HBM/显存归因 (use ascend-memory-profiling), service lifecycle (use vllm-ascend-serving), benchmarks (use vllm-ascend-benchmark), or采集 profiling 数据 (use ascend-profiling-collection).
---

# Ascend Profiling Analysis

Canonical skill source:

`.agents/skills/ascend-profiling-analysis/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use the remote-dev companion tools (`remote_*` MCP tools; CLI fallback `remote-dev <hyphen-tool> ...` or `uv run remote-dev <hyphen-tool> ...`) for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
