---
name: ascend-memory-profiling
description: Profile and attribute HBM memory usage on Ascend NPU for vLLM serving scenarios. Breaks down memory into fixed overhead, model weights, KV cache, HCCL buffers, activations, and runtime, with traceable evidence chains. Use for requests like "分析显存占用", "显存 profiling", "HBM 用了多少", "内存各部分拆分". Do not use for performance profiling (kernel timing, throughput), offline inference, or non-Ascend hardware.
---

# ascend-memory-profiling

Attribute serving HBM to fixed overhead, weights, KV cache, HCCL, activations and runtime with explicit evidence.

Prefer measured msprof and npu-smi evidence, then startup logs and tensor headers. Header byte sizes are exact; per-device sharding and component labels may be inferred. Model-config estimates are a fallback. Keep residual memory visible instead of forcing categories to balance.

## Agent entry

Run from the repository root using the platform's Python launcher. The workspace
selects its installed platform environment automatically.

```text
python .agents/skills/ascend-memory-profiling/scripts/mem_collect.py --help
```

mem_collect.py accepts the serving workload and captures evidence through the managed service. mem_analyze.py consumes the collection output. Keep collection parameters tied to the user question; scripts own lifecycle and report generation.

Use profiling analysis for kernel timing, bubbles and communication latency.

Read the relevant detail only when needed:

- [methodology](references/methodology.md)
- [msprof_fields](references/msprof_fields.md)
