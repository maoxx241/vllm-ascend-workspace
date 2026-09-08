# Behavior contract

## Source boundaries

- `.agents/knowledge/` is the project layer: the only formal, tracked project
  knowledge source. It holds both generations — v1 `<kind>.yaml` and federated
  v2 `<kind>.v2.yaml`.
- `.vaws-local/knowledge/candidates/` is an untracked review queue.
- `.vaws-local/knowledge/reviewed/` is an untracked disposition audit.
- The `shared` layer is the corpus inside the installed `vaws-knowledge`
  package. Never written by this repo.
- `.vaws-local/knowledge/export/` holds proposal bundles plus the export
  ledger used for upstream idempotency.
- Codex local Memories remain personal generated state and never override formal
  workspace knowledge.

## Layer trust

| Layer | Trust | Written by |
|-------|-------|------------|
| `shared` | reviewed by the commons | upstream only, pulled down |
| `project` | reviewed in this repo | this Skill |
| `candidate` | one unreviewed observation | capture |

A query answer names the layer it came from. A missing layer is reported, not
silently skipped, and an empty result means *unknown* — never *supported*.

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

### v2 (default)

`promote` writes `.agents/knowledge/<kind>.v2.yaml`:

```text
uuid            derived from origin repo + kind + slug (stable across reruns)
slug            the entry id
content_hash    sha256 over the canonicalized scope+body payload
                (body is `rule` or `measurement`, keyed by its own name)
status          always 'unverified' on promotion
confidence      candidate confidence; 'high' downgraded to 'medium'
scope           12 dimensions, each bounded or explicitly unresolved
provenance      contributor, origin repo, submitted_at, redaction profile
lifecycle       first_seen, updated_at, superseded_by, resolved_by
rule            failure-signature body: summary, symptom, root_cause,
                resolution, avoidance, fingerprints
measurement     quantity body: summary, subject, method, quantities,
                notes. A number has no symptom. Candidate `promote`
                still writes a `rule`; measurement entries arrive
                from the shared corpus or an explicit body.
verification    only when evidence is followable *and* the captured
                environment covers every concrete dimension
```

Coordinate mapping from the candidate's captured environment:

- a concrete value becomes `{"values": ["<value>"]}` — the claim holds where
  it was observed, and nowhere else by default;
- `unknown` becomes `{"unresolved": true, "needs": "<what a human must
  supply>"}`, which blocks `verified` and blocks export;
- nothing becomes `any`. Independence is a claim, so `resolve --any-basis`
  requires stating what was examined.

Evidence that no reviewer can follow (a local log path, a scratch directory)
is dropped and reported under `dropped_evidence` rather than relabelled.

v2 has no field for a deprecation reason, so `deprecate` records it in
`.vaws-local/knowledge/reviewed/<slug>.deprecation.json` and sets only
`status` and `lifecycle.superseded_by` in the document.

### v1 (`--schema 1`, legacy)

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

`merge` operates on v1 entries only.

## Verification gate (v2)

`verify` refuses to run unless:

- every coordinate dimension is resolved;
- at least one evidence reference has a v2 type (`run_manifest`,
  `pull_request`, `issue`, `commit`, `ci_run`);
- at least one `verified_by` handle is present and it is not only the
  submitter;
- `verified_against` carries concrete values for soc, cann, driver, torch,
  torch_npu, vllm and vllm_ascend.

## Export gate

`.agents/scripts/knowledge_export.py` is the only path upstream. It refuses
unresolved coordinates, re-stamps provenance, recomputes `content_hash`,
validates against the v2 egress whitelist, and applies the full redaction
ruleset including `export`-severity findings (internal mounts, container
instance names, ticket ids) that are legal in the project layer. Re-exporting
an unchanged `uuid` + `content_hash` is a reported no-op, so one fact does not
become two upstream PRs.

## Failure behavior

- Validate before writing.
- Write formal documents and review archives atomically.
- Archive a candidate only after the formal write succeeds.
- Return one JSON document on stdout.
- Return validation failures as JSON with a nonzero exit status.
