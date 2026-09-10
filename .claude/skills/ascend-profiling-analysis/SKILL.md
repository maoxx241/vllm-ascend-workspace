---
name: "ascend-profiling-analysis"
description: "Analyze Ascend NPU torch profiler output (kernel_details.csv / trace_view.json / op_summary / communication.json) for one or many profiling roots and produce a traceable report (rank/step/layer/operator summary, cross-rank alignment, diagnosis findings, report.md / report.xlsx / report.html with single-step inspectors, bubble tracing axes, and zoomable Chrome-tracing-style timelines). Use for requests like \"分析 profiling\", \"解析这份 kernel_details\", \"看 step/layer 切分\", \"跨 rank 对齐\", \"通信慢/EP 不均/快慢卡\", \"生成 profiling 报告\". Do not use for HBM/显存归因 (use ascend-memory-profiling), service lifecycle (use vllm-ascend-serving), benchmarks (use vllm-ascend-benchmark), or采集 profiling 数据 (use ascend-profiling-collection)."
---

<!-- Generated from .agents/skills/ascend-profiling-analysis/SKILL.md. Do not edit. -->

# Ascend Profiling Analysis

Read `.agents/skills/ascend-profiling-analysis/SKILL.md` and only the references needed
for the current task. The canonical skill owns the workflow.
