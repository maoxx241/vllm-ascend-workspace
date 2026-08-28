# Ascend Triton optimization acceptance

## Preconditions and parity

- [ ] Starting kernel has a passed full-case validation manifest.
- [ ] Target environment and starting kernel hash are recorded.
- [ ] NPU baseline covers every case under one measurement policy.
- [ ] Compile/warmup is excluded and raw measurements are preserved.

## Iteration integrity

- [ ] Each round states one hypothesis, change, and expected profiler signal.
- [ ] Parent hash equals the current best hash.
- [ ] Every measured candidate passed the full correctness matrix first.
- [ ] KEEP/DISCARD/NOISE/FAIL came from the controller thresholds.
- [ ] Best code was never overwritten by a discarded candidate.
- [ ] Repeated failures triggered diagnosis rather than repetition.

## Delivery

- [ ] Per-case and aggregate results include the noise and regression policy.
- [ ] Best hash, original baseline improvement, target, and target status are explicit.
- [ ] GPU timing is labeled cross-platform context only.
- [ ] Profiler artifacts support the claimed mechanism, not merely the latency delta.
