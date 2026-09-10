# Graph debug acceptance

Do not mark a graph-debug case resolved until every required item passes.

## Baseline and classification

- [ ] The exact workspace snapshot, environment, model, topology, input, seed, and sampling parameters are recorded.
- [ ] Eager passes under the same functional inputs, or the issue has been routed away from graph debug.
- [ ] The failure is classified as compile, capture, replay, accuracy, or explicitly unknown.
- [ ] The smallest current reproduction is recorded.

## Controlled experiments

- [ ] Each experiment changes one named variable.
- [ ] Each experiment records a falsifiable hypothesis and expected observation before execution.
- [ ] Actual observation, conclusion, and next step are recorded.
- [ ] Previously excluded paths are not repeated without new evidence.

## Snapshot comparison

- [ ] Graph and eager snapshots use identical `step/layer/rank/tag` keys.
- [ ] Snapshot buffers are allocated before capture.
- [ ] No CPU read, synchronization, or file I/O occurs inside captured execution.
- [ ] Tolerances are explicit and justified.
- [ ] The first divergent key is recorded and used to narrow subsequent instrumentation.
- [ ] `compare` consumed a comparable observational certificate from recorded snapshot identities; snapshots without identity, or with a declaration/observation mismatch, are refused.

## Resolution

- [ ] Root cause and fix are recorded.
- [ ] Actual rerun output backs each `pass`; `record` bookkeeping is optional when that evidence exists.
- [ ] The minimal reproduction passes after the fix, and its rerun output is attached with `--minimal-evidence`.
- [ ] The original reproduction passes after the fix, and its rerun output is attached with `--original-evidence`.
- [ ] Temporary buffers, logging, synchronization, deterministic overrides, and workarounds are removed or intentionally disabled.
- [ ] `case.json`, comparison artifacts, `validation/` evidence, and Run Manifest v1 validate, and the manifest links the evidence.
- [ ] Remaining untested combinations are listed as risks rather than implied supported.
