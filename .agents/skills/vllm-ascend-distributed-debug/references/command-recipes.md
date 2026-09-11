# Agent call

From the repository root:

```text
python .agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py --config topology.json --events rank-events.jsonl
```

The config supplies expected_world_size, ranks and optional groups/endpoints. Event files supply observed facts. The report validates mappings and event order and generates its evidence automatically; no case initialization or event-registration steps are required.

Use `--help` for exact argument details. Report output directories are optional
where supported; the script creates a fresh directory under `.vaws-local/`.
Schema versions and report identifiers are generated internally. Input files
describe business cases or contain observed results, rather than task ownership.
