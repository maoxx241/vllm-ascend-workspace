---
name: vllm-ascend-graph-debug
description: Diagnose vLLM Ascend cudagraph and ACL Graph compile, capture, replay, hang, and graph-versus-eager correctness problems. Use when eager passes but graph mode fails, hangs, or diverges, or when graph/eager intermediate tensors must be aligned. Do not use to plan a correctness matrix, after the failure is reduced to one operator, when eager itself fails, or for performance profiling or HBM attribution.
---

# vllm-ascend-graph-debug

Locate the first divergence between eager and graph executions or investigate graph compile, capture and replay failures.

Fix inputs and compare corresponding stages, ranks and steps. Separate compile, capture and replay; instrumentation that synchronizes the device can change the failure. Use bounded tensor capture only when existing outputs cannot distinguish the hypothesis.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
uv run --no-project python .agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py --eager eager.jsonl --graph graph.jsonl
```

Snapshot identity is read from sidecars, or --eager-identity and --graph-identity. The report compares observed identities and samples with finite tolerances, emits the first divergence and a comparability certificate, and retains missing identity as inconclusive. Its conclusion applies to the supplied snapshots.

Use correctness-validation to establish the reproduction, tensor-dump to capture intermediate stages, and operator-debug after reducing to one call.

Read the relevant detail only when needed:
