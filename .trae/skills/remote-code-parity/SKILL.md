---
name: remote-code-parity
description: Ensure the remote runtime runs the exact current local workspace state before any remote smoke test, service launch, or benchmark. Triggered automatically before remote execution when direct local-to-container SSH works. Do not use for initial machine attach, generic Git topology work, or unrelated local tasks.
---

# remote-code-parity

Thin routing stub — the full skill definition lives at `.agents/skills/remote-code-parity/SKILL.md`. Read that file for complete rules, decision gates, and workflow steps.

Quick entry point:

```bash
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py
```
