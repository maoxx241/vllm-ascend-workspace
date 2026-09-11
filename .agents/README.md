# Workspace skills and client wiring

The workspace keeps project materials, installation/client wiring and business
skills. Runtime behavior belongs to the four installed components; see
[AGENTS.md](../AGENTS.md) and [target-state.md](../docs/target-state.md).

- `skills/repo-init/` initializes or repairs workspace configuration and clients.
- `skills/npu-fleet-monitor/` starts, checks or stops the local uvx monitor.
- Other `skills/` directories add vLLM-Ascend business methods such as serving,
  measurements, profiling and debugging. Their `SKILL.md` files are the source
  of truth; the client skill catalog provides discovery.
- `scripts/vaws.py` forwards session/run/execution/finish to coordinator.
  Bind actual worktrees through session; use native Git for source inspection.
- `scripts/workspace_profile.py` and repo-init manage the local username
  document. Provisioning and runtime ownership remain in coordinator.
- Direct remote I/O and optional source publication use their installed owner
  APIs. Managed runs prepare their bound sources internally.
- Knowledge lookup and capture use the package tools. `scripts/knowledge_setup.py`
  configures project Markdown, candidate storage and client wiring.

For explicit knowledge editing, read the installed package skill through its
configured interpreter with `python -m vaws_knowledge skill`.
The optional `curate-knowledge` skill is shipped by that package, not maintained
in this workspace. Native clients can install it into a chosen skill directory
with the package command's `--install-dir` option. Ordinary lookup, capture and
task completion require no curation skill.

## Maintaining skills

Keep names/descriptions specific enough for discovery. Put task decisions and
common entry points in SKILL.md; put detailed formats and conditional procedures
in linked references. Do not duplicate package APIs or add a compulsory
management workflow before business work.

`.claude/skills/` contains generated routing shims. ModelScope's Trae package is
also generated; remaining Trae stubs link to their canonical skill. Regenerate
with `python3 .agents/scripts/sync_claude_skills.py` and verify with `--check`.
`python3 .agents/scripts/skill_catalog.py --help` lists catalog checks.

Local control-plane tests belong in `.agents/tests/` or the owning business
skill. Preserve caller coverage when moving code out of a retired skill; device
execution and model tests still require remote Ascend hardware.
