# Distributed debug acceptance

## Evidence

- [ ] Original world size and every parallel rank coordinate are recorded.
- [ ] Rank-to-node and rank-to-device mappings are explicit.
- [ ] Process groups list exact members.
- [ ] Environment, process tree, endpoints, and reproduction command are saved.
- [ ] Raw rank logs and stack dumps are preserved.
- [ ] Every missing rank is reported as an evidence gap.

## Diagnosis

- [ ] Collective findings name group, sequence, operation, and ranks.
- [ ] Confirmed findings are separated from candidates and evidence gaps.
- [ ] Each experiment changes one topology or runtime variable.
- [ ] The smallest reproducer retains the failure signature.

## Fix validation

- [ ] A regression test covers the proved invariant when practical.
- [ ] The smallest reproducer passes after the fix.
- [ ] The original topology passes after the fix.
- [ ] Temporary debug instrumentation is removed or explicitly retained.
