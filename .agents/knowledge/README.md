# Workspace knowledge store

This directory is the **project layer** of a three-layer knowledge model:

| Layer | Location | Trust | Written by |
|-------|----------|-------|------------|
| `shared` | `.vaws-local/knowledge/shared/` (untracked, read-only cache of `corpus/verified/` from a declared `vaws-knowledge` source) | reviewed by the commons | upstream only; local import refuses project/unverified zones and binds an importer-owned source policy that query/get apply |
| `project` | this directory | reviewed in this repo | `curate-workspace-knowledge` |
| `candidate` | `.vaws-local/knowledge/candidates/` (untracked) | one unreviewed observation | `knowledge_capture.py` |

## Two generations, side by side

- `<kind>.yaml` — the original v1 documents. Applicability is the free-text
  `applicable_versions` field. Kept readable so existing consumers keep
  working; still validated on every run.
- `<kind>.v2.yaml` — the federated v2 contract. Applicability is a structured
  `scope` with twelve dimensions, each either bounded (`values` / `range`) or
  an explicit `any` plus a stated basis. Free text cannot be matched against a
  consumer's actual checkout, which is how a result obtained on one SoC ends
  up applied to a different one.

New writes go to v2 (`knowledge_curate.py promote`); `--schema 1` still writes
the v1 envelope when an entry has to stay readable to a v1-only consumer.

## Unresolved coordinates

A v2 dimension that nobody established carries a local marker:

```json
{"unresolved": true, "needs": "the CANN version of the container the fix was verified on"}
```

This marker is **project-layer only**. It is not valid upstream, and by design
it blocks `status: verified` and blocks export. `MIGRATION-v2.md` lists, per
migrated entry, exactly what a human still has to supply; the same list is
available from:

```bash
python3 .agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py \
  list-unresolved
```

## Rules

- Populate these files only through reviewed project changes. Local candidates
  belong under the ignored `.vaws-local/knowledge/` tree until explicitly
  promoted or merged with stable evidence.
- Never guess a coordinate. `unknown` is a reportable state; a plausible CANN
  or driver version is the confident-but-wrong knowledge this design exists to
  prevent.
- Never write an internal address, hostname, user path, or credential here.
  `knowledge_validate.py` fails on those (`block` severity). Internal mount
  paths and container instance names are legal here and refused on export
  (`export` severity).
- Propose upstream only through `.agents/scripts/knowledge_export.py`.

## Shared conformance kit

The client adapter is `.agents/tests/knowledge_client_adapter.py`. The pinned
kit commit is `.agents/deps/vaws-knowledge.json`. Configure
`VAWS_KNOWLEDGE_KIT_ROOT` to a checkout of that commit, or write the path to
untracked `.vaws-local/knowledge-kit-root`, then:

```bash
python3 .agents/tests/knowledge_kit.py --kit-root <checkout-of-e04d50f7>
```

Unconfigured local unittest runs skip the kit suite with that exact message.
A configured path must be a clean Git checkout or worktree of that commit
(``.git`` may be a file). A missing path, non-Git tree, wrong revision, or
dirty executed runner/vector bytes fails. The kit is an external test input,
not a runtime dependency.
