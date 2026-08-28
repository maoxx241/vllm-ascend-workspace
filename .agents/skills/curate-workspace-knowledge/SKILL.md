---
name: curate-workspace-knowledge
description: Review, deduplicate, promote, merge, reject, or deprecate verified vLLM Ascend workspace knowledge candidates. Use only when the user explicitly asks to curate, persist, review, merge, promote, or deprecate project knowledge (沉淀、整理、复盘、合并、提升、废弃), or explicitly invokes this Skill to review `.vaws-local/knowledge/candidates`. Do not use during normal diagnosis, serving, benchmarking, profiling, remote execution, code review, or candidate capture/query; those workflows call the shared scripts directly without loading this Skill.
---

# Curate Workspace Knowledge

Keep `.agents/knowledge/` as the only formal project knowledge source. Treat
`.vaws-local/knowledge/candidates/` as an untracked review queue, never as a
second authoritative store.

## Workflow

1. Run `scripts/knowledge_curate.py list`.
2. Inspect one candidate and its possible formal matches.
3. Check that the root cause is confirmed, the original symptom was rerun, and
   at least one stable test, commit, issue, or PR evidence item exists.
4. Choose exactly one disposition:
   - `promote` a novel candidate;
   - `merge` it into an existing entry with the same cause and scope;
   - `reject` an unsupported, transient, secret-bearing, or duplicate candidate;
   - `deprecate` a stale formal entry.
5. Run `.agents/scripts/knowledge_validate.py` and the owning Skill's tests.
6. Commit the formal knowledge change together with any regression protection.

## Entry point

`scripts/knowledge_curate.py` provides:

- `list`: return compact candidate summaries;
- `inspect`: return one full candidate plus possible formal matches;
- `promote`: create one `experimental` or `active` formal entry;
- `merge`: merge evidence and occurrences into an existing formal entry;
- `reject`: archive a candidate locally without changing formal knowledge;
- `deprecate`: retain a formal entry while marking it obsolete.

Read only the reference needed for the active operation:

- [Behavior contract](references/behavior.md)
- [Command recipes](references/command-recipes.md)
- [Acceptance](references/acceptance.md)

## Rules

- Never parse or persist a full transcript.
- Never promote `inconclusive` verification.
- Never promote knowledge supported only by untracked or unstable evidence.
- Require a regression test or two verified occurrences before `active`.
- Prefer `merge` over a new entry when cause and applicability match.
- Use `--force-new` only after reviewing an identical fingerprint with a
  different confirmed cause.
- Keep deterministic behavior in the owning Skill's scripts and tests; store
  only the cross-session explanation, scope, fingerprints, and evidence here.
- Do not copy upstream model-adapter lessons or profiler-local counterexamples
  into workspace knowledge unless the new record adds workspace-specific scope
  and references the upstream source.
