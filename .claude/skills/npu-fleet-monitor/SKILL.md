<!-- Generated Claude Code shim from .agents/skills/npu-fleet-monitor/SKILL.md. Do not edit. -->
---
name: npu-fleet-monitor
description: Start, inspect, restart, or stop the loopback-only vaws-top NPU fleet monitor as a local uvx process, and provide its basic CLI/MCP query entrypoints. Use when the local dashboard is needed, for basic fleet discovery and server inspection, or to check whether the monitor is up. Do not use to allocate NPUs, choose a task identity, kill processes, or treat fleet inventory as authority. Detailed fleet-query guidance lives in the standalone vaws-top repository skill.
---

# vaws-top entry

Canonical skill source:

`.agents/skills/npu-fleet-monitor/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use the remote-dev companion tools (`remote_*` MCP tools; CLI fallback `remote-dev <hyphen-tool> ...` or `uv run remote-dev <hyphen-tool> ...`) for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
