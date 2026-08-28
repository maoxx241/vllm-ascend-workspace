---
name: vllm-ascend-distributed-debug
description: Diagnose vLLM Ascend multi-rank and multi-node startup, rank mapping, process-group, collective, HCCL, Ray, scheduler, connector, and distributed hang failures from structured topology and per-rank evidence. Use when a failure depends on rank count, parallel topology, nodes, collectives, or distributed endpoints. Do not use for graph-only divergence, isolated operator failures, performance benchmarking, or profiler analysis.
---

# vLLM Ascend Distributed Debug

Build a falsifiable diagnosis from rank-aware evidence. Never infer a distributed
root cause from one rank's log alone.

## Workflow

1. Create a case with `scripts/distributed_debug.py init`.
2. Capture the exact failing topology, environment, process tree, endpoints, and
   reproduction command before changing parallelism.
3. Add structured per-rank events with `ingest`. Keep raw rank logs and stack
   dumps in the case directories created by `init`.
4. Run `analyze` to check rank identity, group membership, endpoint collisions,
   collective order, missing participants, entered-without-exit stalls, and
   cross-rank phase divergence.
5. Form one or more falsifiable hypotheses from the report.
6. Reduce one parallel dimension at a time. Record each reduced case separately.
7. After a fix, rerun both the smallest reproducer and the original topology.

## Entry point

`scripts/distributed_debug.py` provides:

- `init`: validate the topology contract and create the complete evidence layout;
- `ingest`: validate and append normalized rank events;
- `analyze`: produce deterministic findings, per-rank last progress, and a Run
  Manifest-linked report.

Read only the reference needed for the current phase:

- [Behavior contract](references/behavior.md)
- [Command recipes](references/command-recipes.md)
- [Acceptance](references/acceptance.md)

## Boundaries

- This skill owns failures whose explanation requires comparing ranks, groups,
  nodes, or distributed endpoints.
- Correct outputs in eager mode with graph-only failure belong to
  `vllm-ascend-graph-debug`.
- A failure reduced to one operator call belongs to `ascend-operator-debug`.
- Kernel timing, throughput, and imbalance quantification belong to profiling or
  performance skills; do not load them until the distributed failure is stable
  and the user asks for that evidence.

## Rules

- Preserve raw evidence; normalize into new files rather than rewriting logs.
- Treat missing ranks as missing evidence, not proof that those ranks crashed.
- Treat a collective mismatch as confirmed only when group, sequence, operation,
  and participating ranks are explicit.
- Redact secrets before storing environment snapshots.
- Keep cases under `.vaws-local/distributed-debug/`.
