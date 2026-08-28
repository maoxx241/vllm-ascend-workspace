<!-- Generated Claude Code shim from .agents/skills/curate-workspace-knowledge/SKILL.md. Do not edit. -->
---
name: curate-workspace-knowledge
description: Review, deduplicate, promote, merge, reject, or deprecate verified vLLM Ascend workspace knowledge candidates. Use only when the user explicitly asks to curate, persist, review, merge, promote, or deprecate project knowledge (沉淀、整理、复盘、合并、提升、废弃), or explicitly invokes this Skill to review `.vaws-local/knowledge/candidates`. Do not use during normal diagnosis, serving, benchmarking, profiling, remote execution, code review, or candidate capture/query; those workflows call the shared scripts directly without loading this Skill.
---

# Curate Workspace Knowledge

Canonical skill source:

`.agents/skills/curate-workspace-knowledge/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use `.remote-dev` companion tools for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
