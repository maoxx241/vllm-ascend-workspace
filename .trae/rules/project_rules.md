# Project Rules

Use `AGENTS.md` at the repository root for routing, authorization boundaries,
and maintenance rules. Canonical skills live in `.agents/skills/`; read the
selected `SKILL.md` before invoking it and supporting references as needed.
`.agents/README.md` documents the layout and script conventions.

Read a submodule's own `AGENTS.md` when working in that submodule. Ordinary
local edits do not require loading all skill packages or remote setup.

## Submodule awareness

- `vllm/` and `vllm-ascend/` are Git submodules checked out at arbitrary versions. Never assume a specific commit or directory layout inside them.
- Submodules may be uninitialized (empty directories); do not attempt to read their internal files when this is the case.
- Identically-named symbols across submodules may have different semantics.

## Commit conventions

- Scaffold repo: `<type>: <summary>` (feat / fix / refactor / docs / chore / test).
- Commits inside `vllm-ascend/` follow its own `AGENTS.md` format.

## Skill package maintenance

When modifying any skill under `.agents/skills/`, keep SKILL.md, scripts/, and references/ in sync. See the Maintenance rule section in `AGENTS.md`.
