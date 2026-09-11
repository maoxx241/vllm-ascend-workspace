# Agent call

From the repository root:

```text
python .agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py --eager eager.jsonl --graph graph.jsonl
```

Snapshot identity is read from sidecars, or --eager-identity and --graph-identity. The report compares observed identities and samples with finite tolerances, emits the first divergence and a comparability certificate, and retains missing identity as inconclusive. Its conclusion applies to the supplied snapshots.

Use `--help` for exact argument details. Report output directories are optional
where supported; the script creates a fresh directory under `.vaws-local/`.
Schema versions and report identifiers are generated internally. Input files
describe business cases or contain observed results, rather than task ownership.
