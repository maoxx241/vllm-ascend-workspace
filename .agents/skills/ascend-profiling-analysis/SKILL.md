---
name: ascend-profiling-analysis
description: Analyze Ascend NPU torch profiler output (kernel_details.csv / trace_view.json / op_summary / communication.json) for one or many profiling roots and produce a traceable report (rank/step/layer/operator summary, cross-rank alignment, diagnosis findings, report.md / report.xlsx / report.html with single-step inspectors, bubble tracing axes, and zoomable Chrome-tracing-style timelines). Use for requests like "分析 profiling", "解析这份 kernel_details", "看 step/layer 切分", "跨 rank 对齐", "通信慢/EP 不均/快慢卡", "生成 profiling 报告". Do not use for HBM/显存归因 (use ascend-memory-profiling), service lifecycle (use vllm-ascend-serving), benchmarks (use vllm-ascend-benchmark), or采集 profiling 数据 (use ascend-profiling-collection).
---

# ascend-profiling-analysis

Analyze existing Ascend profiler data and produce evidence-linked step, layer, operator and cross-rank findings.

Tie findings to actual rank/time/row evidence and retain uncertainty in model structure or hardware context. Use config.json or verified profile-visible evidence for model dimensions. Contextual diagnoses belong in project knowledge; deterministic classification and validation belong in the analyzer code or policy data with tests.

## Agent entry

Run from the repository root using the platform's Python launcher. The workspace
selects its installed platform environment automatically.

```text
python .agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py --manifest collection/manifest.json
```

Use --remote-profile-root for an existing root and profile_sweep.py for multiple roots. The normal fast mode returns analysis_summary.json and compact report artifacts. --mode full adds detailed HTML/XLSX outputs. Remote parsing keeps large traces near their storage; explicit remote endpoints and execution references select the analysis target.

Use profiling-collection only when new traces are needed. HBM component attribution belongs to memory-profiling.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)
