# Agent call

From the repository root:

```text
uv run --no-project python .agents/skills/vllm-ascend-serving/scripts/serving.py start --model /models/example --tp 1
```

With user authorization to share one occupied card:

```text
uv run --no-project python .agents/skills/vllm-ascend-serving/scripts/serving.py start --model /models/example --host worker-one --devices 0 --tp 1 --dp 1 --allow-external-busy -- --gpu-memory-utilization 0.1
```

Use serving.py status or serving.py stop with --execution-id or --service. A service reference is resolved by coordinator within the current task. Pending states retain their execution reference. Restart or release follows the requested lifecycle; no separate allocation, parity command or status ledger is needed.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
