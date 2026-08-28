# Ascend Triton workflow acceptance

## Contract

- [ ] Source kind and immutable source identity are recorded.
- [ ] Target SoC and software versions are explicit or marked unknown.
- [ ] Required stages match the requested outcome.
- [ ] Optimization is not scheduled without validation.

## Evidence

- [ ] Each required stage has exactly one linked terminal child manifest.
- [ ] Child run types and parent run IDs match the plan.
- [ ] Correctness evidence precedes performance acceptance.
- [ ] GPU timing is not used as the only NPU performance baseline.
- [ ] Unknown and unsupported combinations remain visible.

## Delivery

- [ ] Parent and child manifests are preserved.
- [ ] Workflow summary and report agree on terminal status.
- [ ] Missing, failed, inconclusive, and optional evidence are explicit.
- [ ] No secrets or full tensor dumps are present in tracked artifacts.
