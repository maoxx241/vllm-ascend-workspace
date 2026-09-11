---
name: vllm-ascend-distributed-debug
description: Diagnose vLLM Ascend multi-rank and multi-node startup, rank mapping, process-group, collective, HCCL, Ray, scheduler, connector, and distributed hang failures from structured topology and per-rank evidence. Use when a failure depends on rank count, parallel topology, nodes, collectives, or distributed endpoints. Do not use for graph-only divergence, isolated operator failures, performance benchmarking, or profiler analysis.
---

# vllm-ascend-distributed-debug

Diagnose failures whose behavior depends on ranks, nodes, process groups, collectives or distributed endpoints.

Start from the failing topology and per-rank timeline. Distinguish missing rank startup, rendezvous, collective ordering and asymmetric workloads. Reduce topology only when the reduced case still reproduces the signature.

## Agent entry

Run from the repository root using the platform's Python launcher. The workspace
selects its installed platform environment automatically.

```text
python .agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py --config topology.json --events rank-events.jsonl
```

The config supplies expected_world_size, ranks and optional groups/endpoints. Event files supply observed facts. The report validates mappings and event order and generates its evidence automatically; no case initialization or event-registration steps are required.

Use graph-debug when eager passes and graph fails independent of topology. Performance imbalance with a successful run belongs to profiling-analysis.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)

- [Business input example](references/inputs.md)
