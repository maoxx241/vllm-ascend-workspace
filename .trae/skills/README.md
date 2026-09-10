# Trae skill entry points

The retained repo-init and serving stubs read their canonical packages under
`.agents/skills/`. ModelScope is a generated projection of its canonical skill.
Use `python3 .agents/scripts/sync_claude_skills.py` after editing ModelScope.

Task binding and managed execution use coordinator tools directly. For explicit
knowledge editing, read the installed skill with
`uv run python -m vaws_knowledge skill` or install it into the native client's
skill directory with `--install-dir`.
