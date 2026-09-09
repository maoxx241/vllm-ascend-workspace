---
name: curate-workspace-knowledge
description: Review, deduplicate, promote, merge, reject, or deprecate verified vLLM Ascend workspace knowledge candidates, resolve unresolved v2 coordinate dimensions, and gate export to the federated commons. Use only when the user explicitly asks to curate, persist, review, merge, promote, deprecate, or upstream project knowledge (沉淀、整理、复盘、合并、提升、废弃、上游), or explicitly invokes this Skill to review `.vaws-local/knowledge/candidate`. Do not use during normal diagnosis, serving, benchmarking, profiling, remote execution, code review, or candidate capture/query; those workflows call the shared scripts directly without loading this Skill.
---

# Curate Workspace Knowledge

Keep `.agents/knowledge/` as the project layer: the only formal, tracked
knowledge this repo owns. Treat `.vaws-local/knowledge/candidate/` as the
only untracked candidate store (commons yaml). Promote reads those yaml
entries and must remove the promoted item so query no longer hits
`layer: candidate`. The `shared` layer is the corpus inside the installed
`vaws-knowledge` package and is never edited here. Query, capture, hash,
and schema checks run on that installed engine; the scripts here are thin
CLIs and curation policy.
Agents can call `knowledge_query` / `knowledge_explain` / `knowledge_capture`
on the `vaws-knowledge` MCP server.

New promotions write the federated **v2** contract to
`.agents/knowledge/<kind>.v2.yaml`. The project layer contains only
`*.v2.yaml`. A v2 entry has exactly one body (`rule` or `measurement`);
candidate promotion still writes a `rule`. Measurement entries are first-class
when listing, querying, hashing, or exporting.

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
5. For a v2 promotion, close the coordinate before claiming anything:
   - `list-unresolved` reports every dimension still waiting on a human;
   - `resolve` fills one dimension from a real run (`--values`), a stated
     independence basis (`--any-basis`), or a bound (`--min` / `--max`);
   - `verify` attaches followable evidence plus a non-submitter confirmation
     and moves the entry to `status: verified`.
6. Only then, if the fact belongs upstream, run
   `.agents/scripts/knowledge_export.py` and open the PR it points at.
7. Run `.agents/scripts/knowledge_validate.py` and the owning Skill's tests.
8. Commit the formal knowledge change together with any regression protection.

## Entry point

`scripts/knowledge_curate.py` provides:

- `list`: return compact candidate summaries;
- `inspect`: return one full candidate plus possible formal matches;
- `promote`: create one v2 entry;
- `reject`: archive a candidate locally without changing formal knowledge;
- `deprecate`: retain a formal entry while marking it obsolete;
- `resolve`: fill one unresolved v2 coordinate dimension;
- `verify`: record independent confirmation for a v2 entry;
- `list-unresolved`: report v2 entries blocked on a human coordinate.

Related shared scripts, outside this Skill:

- `.agents/scripts/knowledge_export.py`: the source-side export gate;
- `.agents/scripts/knowledge_validate.py`: validate the project-layer v2
  documents and report redaction posture.

Read only the reference needed for the active operation:

- [Behavior contract](references/behavior.md)
- [Command recipes](references/command-recipes.md)
- [Acceptance](references/acceptance.md)

## Rules

- Never parse or persist a full transcript.
- Never promote `inconclusive` verification.
- Never promote knowledge supported only by untracked or unstable evidence.
- Require a regression test or two verified occurrences before `active`
  (v1) or before promoting with the `active` evidence gate (v2).
- Never invent a coordinate. A dimension nobody established stays an
  unresolved marker; `any` is a positive claim of independence and needs a
  basis describing what was actually examined.
- A v2 entry reaches `verified` only with a complete coordinate, followable
  evidence, and a confirming handle that is not the submitter.
- Never publish upstream from a raw document. Export only through
  `.agents/scripts/knowledge_export.py`, which strips internal addresses,
  user paths, hostnames, container names and mounts, and refuses unresolved
  coordinates.
- Never write a local copy of the shared corpus; the shared layer is the
  installed `vaws-knowledge` package and flows one way, downward.
- Prefer `merge` over a new entry when cause and applicability match.
- Use `--force-new` only after reviewing an identical fingerprint with a
  different confirmed cause.
- Keep deterministic behavior in the owning Skill's scripts and tests; store
  only the cross-session explanation, scope, fingerprints, and evidence here.
- Do not copy upstream model-adapter lessons or profiler-local counterexamples
  into workspace knowledge unless the new record adds workspace-specific scope
  and references the upstream source.
