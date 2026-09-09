<!-- Generated Claude Code shim from .agents/skills/remote-code-parity/SKILL.md. Do not edit. -->
---
name: remote-code-parity
description: Inspect or publish local workspace sources onto a prepared container work root before direct remote smoke. Do not use for managed coordinator executions, machine bootstrap, or generic Git topology work.
---

# Remote Code Parity

Canonical skill source:

`.agents/skills/remote-code-parity/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use the remote-dev companion tools (`remote_*` MCP tools; CLI fallback `remote-dev <hyphen-tool> ...` or `uv run remote-dev <hyphen-tool> ...`) for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
