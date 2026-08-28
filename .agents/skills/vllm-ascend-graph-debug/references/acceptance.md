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

## Resolution

- [ ] Root cause and fix are recorded.
- [ ] The minimal reproduction passes after the fix.
- [ ] The original reproduction passes after the fix.
- [ ] Temporary buffers, logging, synchronization, deterministic overrides, and workarounds are removed or intentionally disabled.
- [ ] `case.json`, comparison artifacts, and Run Manifest v1 validate.
- [ ] Remaining untested combinations are listed as risks rather than implied supported.
