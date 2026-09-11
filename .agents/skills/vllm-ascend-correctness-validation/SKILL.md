---
name: vllm-ascend-correctness-validation
description: Plan, execute, normalize, and compare vLLM Ascend inference correctness across baseline and candidate code states, eager and graph modes, offline generate or chat, online chat completions, and AISBench task metrics. Use for accuracy validation, token-output comparison, graph-versus-eager checks, deterministic regression testing, or failure classification. Do not use to root-cause an already reproduced graph-only or isolated-operator failure, or for throughput benchmarking, HBM attribution, or profiling-only analysis.
---

# vllm-ascend-correctness-validation

Compare inference outputs across code or execution configurations with explicit comparability and numerical criteria.

Select deterministic prompts or token IDs, sampling, model and topology that exercise the change. Token equality and dataset task metrics answer different questions. Declare only the intended varying dimensions with --allowed-difference.

## Agent entry

Run from the repository root using the platform's Python launcher. The workspace
selects its installed platform environment automatically.

```text
python .agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py --cases cases.json --baseline baseline.json --candidate candidate.json
```

The remote_correctness_harness.py payload captures offline runtime observations from the managed execution. Online/AISBench results use the server execution reference through aisbench_adapter.py. The comparison derives metadata from actual outputs, emits its certificate and report, and reports missing identity as inconclusive.

Route an eager-passes/graph-fails reproduction to graph-debug, a rank-dependent failure to distributed-debug, and a reduced operator failure to operator-debug.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)
- [aisbench](references/aisbench.md)
