<!-- Generated Claude Code shim from .agents/skills/vllm-ascend-multinode-serving/SKILL.md. Do not edit. -->
---
name: vllm-ascend-multinode-serving
description: Plan and bring up one vLLM Ascend data-parallel service spanning several NPU nodes, deriving per-node rank offsets, headless roles, socket-interface and HCCL environment, port allocation, cross-node code-identity checks, and a staged readiness gate. Use when a topology needs more devices than one host provides, when a multi-node service hangs during collective init, or when nodes may be running different code. Do not use for single-node serving, prefill/decode disaggregation, generic Ray clusters, benchmarking, or diagnosing an already-reproduced distributed failure.
---

# vLLM Ascend Multi-Node Serving

Canonical skill source:

`.agents/skills/vllm-ascend-multinode-serving/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use `.remote-dev` companion tools for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
