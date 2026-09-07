---
name: vllm-ascend-serving
description: Start, inspect, restart, or stop a single-node vLLM Ascend online service in a managed session. Use for 拉服务 or 服务状态; not benchmarks.
---

# vllm-ascend-serving

Thin routing stub — the full skill definition lives at `.agents/skills/vllm-ascend-serving/SKILL.md`. Read that file for complete rules, decision gates, and workflow steps.

Quick entry points:

```bash
# Run inside the session worktree, or pass an explicit session id as below.
# Start a service
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --session-id <id> --model <path> --tp <N>

# Check status
python3 .agents/skills/vllm-ascend-serving/scripts/serve_status.py \
  --session-id <id>

# Stop a service
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py \
  --session-id <id>

# Probe NPU availability
python3 .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py \
  --machine <alias-or-ip>
```
