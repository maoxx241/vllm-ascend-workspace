---
name: curate-workspace-knowledge
description: Review and deduplicate Markdown knowledge candidates and prepare redacted contributions through the installed knowledge package. Use only for explicit knowledge review, curation, or upstream contribution requests. Normal capture and query use the shared entry points directly.
---

# Curate Workspace Knowledge

Knowledge is Markdown reference material. Review the explanation, conditions,
source and evidence that are actually present. Preserve uncertainty; a public
review decision does not establish a hardware fact. Do not require v2 status,
UUID, type or a complete runtime coordinate to keep a useful observation.

## Workflow

1. Inspect candidate Markdown in `.vaws-local/knowledge/candidate/` and query
   related material with `.agents/scripts/knowledge_query.py`.
2. Read relevant matches. Edit or combine duplicate local Markdown when the
   evidence supports it. Keep differing conditions and conflicting evidence
   visible. Do not overwrite shared release files.
3. For an authorized upstream contribution, run `knowledge_curate.py prepare`.
   The installed package writes a redacted public copy and a pending record;
   it leaves the source candidate unchanged.
4. Submit only the prepared public copy using the package `submit` command
   with the intended fork, upstream and local Git checkout. Keep pending
   records on transport failure.
5. Use package `review`, `ci`, `resolve` and `merge` for public review. These
   require the real configured recall/classifier and GitHub transport.
   Missing recall or stale Git evidence must not authorize a merge.

## Entry point

`scripts/knowledge_curate.py` delegates directly to
`python -m vaws_knowledge contribution`. Run `--help` or a command's `--help`
for current arguments. The package owns redaction, pending state, review,
conflict handling and Git identity checks; workspace adds no second engine.

The old `promote`, `verify`, `list-unresolved`, and coordinate-editing verbs
are retired. `.agents/scripts/knowledge_validate.py` and
`.agents/scripts/knowledge_export.py` remain maintenance tools for existing
v2 YAML records only; they do not process new Markdown candidates.

Read the applicable reference:

- [Behavior contract](references/behavior.md)
- [Command recipes](references/command-recipes.md)
- [Acceptance](references/acceptance.md)

Never parse or persist a full transcript. Never publish a raw local candidate.
Keep internal addresses, user paths and credentials out of public copies.
