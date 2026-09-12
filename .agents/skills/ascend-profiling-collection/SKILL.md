---
name: ascend-profiling-collection
description: Collect one Ascend torch-profiler case end-to-end on a workspace-managed remote NPU container. Starts a profiled vLLM service, brackets a workload with /start_profile and /stop_profile, runs analyse() (db export by default), verifies the per-rank ascend_pytorch_profiler_*.db landed, and writes a manifest the analysis skill can consume. Use for requests like "采集 profiling", "torch profiler 跑一个 case", "采一份 profile 出来", "采 profiling 给我分析". Do not use for pure performance benchmarking, HBM/memory profiling, or for analysing already-collected profiling data (that is the analysis skill's job).
---

# ascend-profiling-collection

Collect one torch-profiler case, bracket a real workload, export per-rank data and return an analysis-ready manifest.

Choose a capture window and workload that expose the suspected bottleneck. Keep token counts and concurrency representative. For multimodal cases pass the local image and target height; encoding is platform-independent.

Collection starts from the supplied workload and records actual results. It does
not query knowledge before startup or on failure. If related experience would
help, the Agent can use the knowledge MCP tools independently; this is optional.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
uv run --no-project python .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py --model /models/example --served-model-name example --tp 1 --tag case --mode enforce_eager --request-kind text --benchmark-output-tokens 128
```

The workflow starts or observes the managed service, controls /start_profile and /stop_profile, runs analyse(), verifies expected rank outputs and records workload success. DB export is the default. Large traces stay near the data; the resulting manifest can be passed directly to analysis.

Use profiling-analysis for existing traces, memory-profiling for HBM attribution, and benchmark for throughput measurements without tracing.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)
