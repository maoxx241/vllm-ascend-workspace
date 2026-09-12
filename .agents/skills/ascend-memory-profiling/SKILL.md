---
name: ascend-memory-profiling
description: Attribute vLLM serving HBM usage on Ascend to weights, KV cache, HCCL, activations and runtime using measured evidence. Use for 显存归因, 显存 profiling, or 内存各部分拆分. A quick current memory-usage or idle-card lookup uses the fleet monitor; kernel latency analysis uses profiling-analysis.
---

# ascend-memory-profiling

Attribute serving HBM to fixed overhead, weights, KV cache, HCCL, activations and runtime with explicit evidence.

Prefer measured msprof and npu-smi evidence, then startup logs and tensor headers. Header byte sizes are exact; per-device sharding and component labels may be inferred. Model-config estimates are a fallback. Keep residual memory visible instead of forcing categories to balance.

## Agent entry

Run from the repository root using the platform's Python launcher. The workspace
selects its installed platform environment automatically.

```text
uv run --no-project python .agents/skills/ascend-memory-profiling/scripts/mem_collect.py --help
```

mem_collect.py accepts the serving workload and captures evidence through the managed service. mem_analyze.py consumes the collection output. Keep collection parameters tied to the user question. Standalone collection embeds its local msprof wrapper in one managed serving execution, forwards the requested serving configuration, stops that execution, and exports only its recorded runtime directory. Missing CSV evidence returns an incomplete result. Attach mode reuses an execution and leaves a live service running; resume requires the same execution ID.

A ready-state sample and a post-inference sample do not establish an activation peak. Report missing idle baselines as unknown, keep unassigned process-level msprof data separate from device totals, and label tensor-name sharding as an estimate.

Use profiling analysis for kernel timing, bubbles and communication latency.

Read the relevant detail only when needed:

- [methodology](references/methodology.md)
- [msprof_fields](references/msprof_fields.md)
