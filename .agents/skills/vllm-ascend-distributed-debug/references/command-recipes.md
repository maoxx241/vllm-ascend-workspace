# Agent call

From the repository root:

```text
uv run --no-project python .agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py --config topology.json --events rank-events.jsonl
```

The config supplies expected_world_size, ranks and optional groups/endpoints. Event files supply observed facts in capture order. The report checks mappings and structured collective observations; absent events remain capture gaps rather than proof that a rank skipped the collective. No case initialization or event-registration steps are required.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
