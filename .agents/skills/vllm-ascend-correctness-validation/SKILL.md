---
name: vllm-ascend-correctness-validation
description: Run and compare vLLM Ascend inference outputs or accuracy metrics across code states, eager/graph modes, or serving configurations. Use for token comparison, numerical regression checks, and AISBench accuracy evaluation. An already reproduced graph, operator or distributed failure uses its debug workflow; ordinary unit tests and code review use native tools.
---

# vllm-ascend-correctness-validation

Compare inference outputs across code or execution configurations with explicit comparability and numerical criteria.

Select deterministic prompts or token IDs, sampling, model and topology that exercise the change. Token equality and dataset task metrics answer different questions. Declare only the intended varying dimensions with --allowed-difference.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
uv run --no-project python .agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py --cases cases.json --baseline baseline.json --candidate candidate.json
```

The remote_correctness_harness.py payload captures offline runtime observations from the managed execution. Online/AISBench results use the server execution reference through aisbench_adapter.py. The comparison derives metadata from actual outputs, emits its certificate and report, and reports missing identity as inconclusive.

Route an eager-passes/graph-fails reproduction to graph-debug, a rank-dependent failure to distributed-debug, and a reduced operator failure to operator-debug.

Read the relevant detail only when needed:

- [aisbench](references/aisbench.md)
