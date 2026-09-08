<!-- Generated Claude Code shim from .agents/skills/vllm-ascend-performance-regression/SKILL.md. Do not edit. -->
---
name: vllm-ascend-performance-regression
description: Plan, record, and analyze controlled baseline-versus-candidate vLLM Ascend serving performance experiments with isolated sessions, identical non-code configuration, alternating A/B order, warmup exclusion, variance and outlier reporting, and metric-specific regression thresholds. Use for throughput, TTFT, TPOT, ITL, acceptance-rate, startup-time, or HBM regression checks. Do not use for correctness, single-state measurement, HBM component attribution, or profiling root-cause analysis.
---

# vLLM Ascend Performance Regression

Canonical skill source:

`.agents/skills/vllm-ascend-performance-regression/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use the remote-dev companion tools (`remote_*` MCP tools; CLI fallback `python3 .agents/scripts/remote_dev.py tool <name> ...`) for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
