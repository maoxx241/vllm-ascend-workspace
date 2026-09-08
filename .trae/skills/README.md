# Trae skill projections

Trae projections are generated per-skill, not hand-written for the full catalog. The only generic generator in this repository is `python3 .agents/scripts/sync_claude_skills.py`, and it is ModelScope-specific: it copies `.agents/skills/modelscope` byte-for-byte onto `.trae/skills/modelscope`. The other four Trae packages (`machine-management`, `remote-code-parity`, `repo-init`, `vllm-ascend-serving`) are hand-maintained routing stubs. Do not invent the missing 19 projections here; agents should follow `.agents/skills/<name>/SKILL.md`.
