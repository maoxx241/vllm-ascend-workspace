# Ascend Triton development acceptance

## Contract and audit

- [ ] Source, reference, target environment, cases, and tolerances are explicit.
- [ ] Every load, store, mask, index, grid dimension, and side effect is audited.
- [ ] Multi-shape and non-aligned cases were not discarded.
- [ ] Unknown target capabilities are marked unknown.

## Design and implementation

- [ ] Sketch records grid mapping, tiling, UB live-set estimate, padding, and specialization.
- [ ] Core computation is in Triton rather than a PyTorch fallback.
- [ ] First candidate preserves correct padding and numerical identities.
- [ ] Candidate source, audit, and sketch are non-empty and hashed.

## Evidence

- [ ] Static fallback check passed.
- [ ] Every planned correctness case passed on a managed Ascend NPU.
- [ ] Validation used the same reference, cases, and predeclared tolerances.
- [ ] Validation manifest is linked and terminal.
- [ ] No performance claim is made by this Skill.
