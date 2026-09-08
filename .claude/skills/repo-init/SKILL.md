<!-- Generated Claude Code shim from .agents/skills/repo-init/SKILL.md. Do not edit. -->
---
name: repo-init
description: Initialize this workspace after clone. Use for requests like “初始化仓库”, “配置 gh / GitHub 登录”, “初始化子模块”, “uv sync 外部包”, or “把 vllm / vllm-ascend remotes 改成我的 fork”. Do not use for ordinary coding, serving, benchmarking, or unrelated Git tasks.
---

# Repo Init

Canonical skill source:

`.agents/skills/repo-init/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use the remote-dev companion tools (`remote_*` MCP tools; CLI fallback `remote-dev <hyphen-tool> ...` or `uv run remote-dev <hyphen-tool> ...`) for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
