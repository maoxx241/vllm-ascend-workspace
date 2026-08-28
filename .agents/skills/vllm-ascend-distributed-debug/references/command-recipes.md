# Distributed debug command recipes

## Initialize

```bash
python -B .agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py init \
  --output-dir .vaws-local/distributed-debug/case-001 \
  --config /path/to/distributed-case.json
```

Capture raw logs, stack dumps, and metadata samples without rewriting them.

## Ingest normalized events

```bash
python -B .agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py ingest \
  --output-dir .vaws-local/distributed-debug/case-001 \
  --events /path/to/rank-events.jsonl
```

Append events for all ranks. Keep sequences scoped to their named process group.

## Analyze

```bash
python -B .agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py analyze \
  --output-dir .vaws-local/distributed-debug/case-001
```

Use `analysis.json` to select one controlled topology reduction. Do not change
multiple parallel dimensions in the same experiment.
