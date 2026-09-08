---
name: vllm-ascend-serving
description: Start, check, or stop a single-node vLLM Ascend online service on a workspace-managed ready remote container. Triggered when users ask to launch, restart, check status, or stop a vLLM Ascend service. Do not use for machine attach, environment bootstrap, code sync, benchmark orchestration, or offline inference.
---

# vllm-ascend-serving

Thin routing stub — the full skill definition lives at `.agents/skills/vllm-ascend-serving/SKILL.md`. Read that file for complete rules, decision gates, and workflow steps.

Serving is session-only. Inside a session worktree the session is auto-resolved. Outside a worktree, pass `--session-id` or `--session-file`. `--machine` is not a serving target; only `serve_probe_npus.py` still accepts it for host NPU probing.

Quick entry points:

```bash
# Start a service
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  [--session-id <id> | --session-file <path>] --model <path> --tp <N>

# Check status
python3 .agents/skills/vllm-ascend-serving/scripts/serve_status.py \
  [--session-id <id> | --session-file <path>]

# Stop a service
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py \
  [--session-id <id> | --session-file <path>]

# Probe NPU availability (legacy host probe still accepts --machine)
python3 .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py \
  [--session-id <id> | --machine <alias-or-ip>]
```
