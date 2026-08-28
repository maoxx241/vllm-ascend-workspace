# Change validation acceptance

## Diff and impact

- [ ] Baseline, candidate, target repositories, and user goal are recorded.
- [ ] The diff includes relevant staged, unstaged, and untracked changes.
- [ ] Cross-repository changes use a combined diff or equivalent complete evidence.
- [ ] Every matched impact links to a versioned Skill-owned rule or an explicit reviewed knowledge override.
- [ ] Unclassified changes are manually reviewed.

## Plan

- [ ] Every item records priority, rationale, and source.
- [ ] Required items have not been silently downgraded for resource reasons.
- [ ] The plan was reviewed before expensive NPU execution.
- [ ] Omitted combinations are listed as limitations.

## Evidence

- [ ] Each linked run has a valid Run Manifest.
- [ ] Each link names the plan item IDs it covers.
- [ ] Failed, inconclusive, cancelled, running, and planned children are not treated as passing.
- [ ] Correctness, performance, graph, distributed, operator, and profiling evidence use their owning workflows.

## Delivery

- [ ] Every required plan item has passed evidence before parent status is passed.
- [ ] `pr-validation-report.md` contains change summary, impact, validation matrix, results, limitations, reproduction, and artifact locations.
- [ ] Parent Run Manifest validates and links all child manifests.
