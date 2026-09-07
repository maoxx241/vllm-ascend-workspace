<!-- Generated Claude Code shim from .agents/skills/session-management/SKILL.md. Do not edit. -->
---
name: session-management
description: Associate native agent sessions with local VAWS development tasks, bind actual business worktrees, and coordinate prepared remote runtimes and NPU leases. Also create, inspect, remove, and group legacy container-bound sessions. Use for parallel task isolation and session ownership, not model correctness or distributed failure diagnosis.
---

# Session Management

Canonical skill source:

`.agents/skills/session-management/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use the remote-dev companion tools (`remote_*` MCP tools; CLI fallback `python3 .agents/scripts/remote_dev.py tool <name> ...`) for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
