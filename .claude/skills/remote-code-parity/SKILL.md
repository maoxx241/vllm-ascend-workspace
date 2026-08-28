<!-- Generated Claude Code shim from .agents/skills/remote-code-parity/SKILL.md. Do not edit. -->
---
name: remote-code-parity
description: Ensure a ready remote runtime runs the exact current local workspace state before any remote smoke, service launch, or benchmark. Use automatically immediately before remote execution when direct local-to-container SSH already works and local uncommitted changes must be reflected remotely. Do not use for initial machine attach, generic Git topology work, or unrelated local-only coding.
---

# Remote Code Parity

Canonical skill source:

`.agents/skills/remote-code-parity/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use `.remote-dev` companion tools for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
