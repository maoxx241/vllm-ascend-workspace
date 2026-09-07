<!-- Generated Claude Code shim from .agents/skills/vllm-ascend-experiment-ledger/SKILL.md. Do not edit. -->
---
name: vllm-ascend-experiment-ledger
description: Index every Run Manifest v1 run across the workspace, show which code state and topology produced a given result, and decide whether two runs differ only in the variable under test. Use when several experiments have accumulated, when a number cannot be traced to the code that produced it, when deciding whether an A/B pair is a fair comparison, or before repeating a diagnosis that may already have been done. Do not use to execute runs, to judge whether a metric regressed, or as a substitute for the owning execution Skill's own state.
---

# vLLM Ascend Experiment Ledger

Canonical skill source:

`.agents/skills/vllm-ascend-experiment-ledger/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use `.remote-dev` companion tools for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
