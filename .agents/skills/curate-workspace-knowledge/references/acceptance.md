# Acceptance

- [ ] The Skill is not implicitly invoked.
- [ ] Normal domain workflows can capture/query without loading this Skill.
- [ ] Formal writes target only one existing `.agents/knowledge/*.yaml` file.
- [ ] Inconclusive or unstable-only candidates cannot be promoted.
- [ ] Active promotion requires repeat evidence or a regression test.
- [ ] Exact fingerprint matches require editing the existing v2 entry,
      promoting a superseding revision, or explicit `--force-new`.
- [ ] Promotion and reject archive the candidate yaml entry and remove it from
      the candidate layer so a later query only hits the project layer.
- [ ] Rejection does not modify formal knowledge.
- [ ] Deprecation retains the entry and records reason/replacement.
- [ ] stdout contains one final JSON document.
- [ ] Shared knowledge validation and owning Skill tests pass.

## Federated v2

- [ ] `promote` writes `<kind>.v2.yaml`.
- [ ] Every unknown coordinate dimension becomes an unbounded range
      (`min` and `max` both null); none becomes `any` or a plausible value.
      The curator hint is returned as `needs_human_input`, not stored in
      `scope`.
- [ ] A promoted entry is `unverified`, and the result names each dimension a
      human must supply.
- [ ] `resolve` accepts exactly one of `--values`, `--any-basis`, or
      `--min`/`--max`, and rehashes the entry.
- [ ] `verify` refuses an unresolved coordinate, an unfollowable reference, a
      submitter-only confirmation, and an incomplete `verified_against`.
- [ ] Evidence that no reviewer can follow is dropped and reported, never
      relabelled.
- [ ] A v2 deprecation reason is archived under `.vaws-local/`, not written
      into the document.
- [ ] Export refuses unresolved coordinates and `export`-severity redaction
      findings, and re-exporting an unchanged entry is a reported no-op.
- [ ] This repo never writes a local copy of the shared `vaws-knowledge` corpus.
