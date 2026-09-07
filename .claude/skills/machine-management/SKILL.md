<!-- Generated Claude Code shim from .agents/skills/machine-management/SKILL.md. Do not edit. -->
---
name: machine-management
description: Add, verify, repair, or remove a managed remote NPU host for this workspace. Use for requests like “配置服务器”, “加一台机器”, “检查 ready”, “修容器 SSH”, or “移除机器”. Do not use for code sync, rebuilds, serving, or benchmarking.
---

# Machine Management

Canonical skill source:

`.agents/skills/machine-management/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use the remote-dev companion tools (`remote_*` MCP tools; CLI fallback `python3 .agents/scripts/remote_dev.py tool <name> ...`) for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
