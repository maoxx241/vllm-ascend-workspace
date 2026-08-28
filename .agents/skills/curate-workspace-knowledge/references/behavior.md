# Behavior contract

## Source boundaries

- `.agents/knowledge/` is the only formal, tracked project knowledge source.
- `.vaws-local/knowledge/candidates/` is an untracked review queue.
- `.vaws-local/knowledge/reviewed/` is an untracked disposition audit.
- Codex local Memories remain personal generated state and never override formal
  workspace knowledge.

## Promotion gates

An `experimental` entry requires:

- verification status `passed`;
- a confirmed root cause and resolution;
- at least one stable evidence item;
- no unresolved exact-fingerprint duplicate.

An `active` entry additionally requires either:

- two verified occurrences; or
- stable `test`, `regression-test`, or `acceptance-test` evidence.

Use `merge` when cause and applicability match. Use `--force-new` only after
confirming that an identical fingerprint has a different cause.

## Formal entry mapping

Promotion keeps the existing formal v1 envelope:

```text
id
source
applicable_versions
updated_at
status
rule
```

The structured candidate fields live inside `rule`, including candidate ids,
scope, fingerprints, cause, resolution, verification, evidence, confidence,
occurrences, and verification dates.

## Failure behavior

- Validate before writing.
- Write formal documents and review archives atomically.
- Archive a candidate only after the formal write succeeds.
- Return one JSON document on stdout.
- Return validation failures as JSON with a nonzero exit status.
