---
name: machine-management
description: Add, verify, repair, or remove a managed remote NPU host for this workspace. Use for requests like "configure server", "add a machine", "check ready", "fix container SSH", or "remove machine". Do not use for code sync, rebuilds, serving, or benchmarking.
---

# machine-management

Thin routing stub — the full skill definition lives at `.agents/skills/machine-management/SKILL.md`. Read that file for complete rules, decision gates, and workflow steps.

Quick entry point:

```bash
python3 .agents/skills/machine-management/scripts/machine_verify.py --name <machine>
```
