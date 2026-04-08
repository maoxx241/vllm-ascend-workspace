---
name: vllm-ascend-serving
description: Start, check, or stop a single-node vLLM Ascend online service on a workspace-managed ready remote container. Triggered when users ask to launch, restart, check status, or stop a vLLM Ascend service. Do not use for machine attach, environment bootstrap, code sync, benchmark orchestration, or offline inference.
---

# vllm-ascend-serving

Thin routing stub — the full skill definition lives at `.agents/skills/vllm-ascend-serving/SKILL.md`. Read that file for complete rules, decision gates, and workflow steps.

Quick entry points:

```bash
# Start a service
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine <alias-or-ip> --model <path> --tp <N>

# Check status
python3 .agents/skills/vllm-ascend-serving/scripts/serve_status.py \
  --machine <alias-or-ip>

# Stop a service
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py \
  --machine <alias-or-ip>

# Probe NPU availability
python3 .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py \
  --machine <alias-or-ip>
```
