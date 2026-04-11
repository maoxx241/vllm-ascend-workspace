# Project Rules

The authoritative agent instructions and skill packages for this workspace are defined in the following locations — always consult them:

1. **`AGENTS.md`** (repo root) — repo-wide routing rules, mandatory decision gates, and maintenance constraints.
2. **`.agents/skills/`** — skill packages (repo-init, machine-management, remote-code-parity, vllm-ascend-serving, vllm-ascend-benchmark, ascend-memory-profiling), each with SKILL.md, scripts/, and references/.
3. **`.agents/README.md`** — skill layout and script-first conventions.
4. Each submodule's own `AGENTS.md` (e.g. `vllm-ascend/AGENTS.md`) — version-specific coding conventions; always defer to the submodule's own file.

## Submodule awareness

- `vllm/` and `vllm-ascend/` are Git submodules checked out at arbitrary versions. Never assume a specific commit or directory layout inside them.
- Submodules may be uninitialized (empty directories); do not attempt to read their internal files when this is the case.
- Identically-named symbols across submodules may have different semantics.

## Commit conventions

- Scaffold repo: `<type>: <summary>` (feat / fix / refactor / docs / chore / test).
- Commits inside `vllm-ascend/` follow its own `AGENTS.md` format.

## Skill package maintenance

When modifying any skill under `.agents/skills/`, keep SKILL.md, scripts/, and references/ in sync. See the Maintenance rule section in `AGENTS.md`.
