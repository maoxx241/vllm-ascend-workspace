# Agent call

From the repository root:

```text
uv run --no-project python .agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py --eager eager.jsonl --graph graph.jsonl
```

Snapshot identity is read from sidecars, or --eager-identity and --graph-identity. The report compares observed identities and samples with finite tolerances, emits the first divergence and a comparability certificate, and retains missing identity as inconclusive. Its conclusion applies to the supplied snapshots.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
