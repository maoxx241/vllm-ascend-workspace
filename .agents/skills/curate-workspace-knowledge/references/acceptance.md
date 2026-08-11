# Acceptance

- [ ] The Skill is not implicitly invoked.
- [ ] Normal domain workflows can capture/query without loading this Skill.
- [ ] Formal writes target only one existing `.agents/knowledge/*.yaml` file.
- [ ] Inconclusive or unstable-only candidates cannot be promoted.
- [ ] Active promotion requires repeat evidence or a regression test.
- [ ] Exact fingerprint matches require merge or explicit `--force-new`.
- [ ] Promotion and merge archive the candidate only after formal validation.
- [ ] Rejection does not modify formal knowledge.
- [ ] Deprecation retains the entry and records reason/replacement.
- [ ] stdout contains one final JSON document.
- [ ] Shared knowledge validation and owning Skill tests pass.
