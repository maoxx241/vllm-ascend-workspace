# Experiment ledger acceptance

## Before quoting a run as a baseline

- [ ] The run is in the index, not only on disk.
- [ ] Its status is terminal.
- [ ] Its `identity` is non-empty and names the repository commits, the runtime,
      the weights, and the topology.
- [ ] The identity was recorded at run creation, not added afterwards.

A run failing any of these may be reported as something that happened. It may
not be reported as a baseline, because a baseline is a claim about what produced
a number.

## Before presenting a comparison

- [ ] `compare` was run and its verdict is stated, not implied.
- [ ] The declared `--vary` keys are the change actually under test.
- [ ] `confounders` is empty.
- [ ] `declared_but_identical` is empty, or the reason a declared key did not
      move is understood.
- [ ] Both runs are terminal.

On a `not-comparable` verdict there are exactly two honest options: hold the
confounders constant and re-run, or present the numbers explicitly as
unattributable. Interpreting the delta anyway is the failure this Skill exists
to prevent.

## Before concluding an experiment has not been run

- [ ] The index was searched by identity, not by directory name.
- [ ] The conclusion says it covers manifest-recorded runs only.

The ledger cannot see runs that never wrote a conforming manifest, and those are
precisely the runs most likely to have been forgotten.

## When reporting the index

- [ ] `unindexed` entries are reported with their reasons, not summarized away.
- [ ] `runs_without_identity` is reported.
- [ ] The number of recoverable runs is distinguished from the number of
      directories present.

## What this Skill does not establish

- Whether a metric regressed. Thresholds, variance, and ordering belong to
  `vllm-ascend-performance-regression`.
- Whether a change is correct. That is
  `vllm-ascend-correctness-validation`.
- What evidence a change requires. That is
  `vllm-ascend-change-validation`.
- Whether the runtime currently matches a run's recorded identity. The ledger
  reports what was recorded; confirming the live runtime still matches it
  requires a fresh probe against that runtime.
