# Ascend Triton profiling decision tree

## Contents

- Establish floors
- Classify the bottleneck
- Choose the next experiment
- Stop conditions

## Establish floors

Estimate before editing:

```text
memory_floor = (GM read bytes + GM write bytes) / effective GM bandwidth
vector_floor = vector elements / effective vector throughput
launch_floor = measured empty or tiny kernel cost
```

Use target-specific effective values rather than marketing peaks. Save device
time, wrapper time, block count, and per-pipeline evidence.

## Classify the bottleneck

| Evidence | Likely bottleneck | First experiment |
|---|---|---|
| Blocks greatly exceed usable physical cores; dispatch visible | repeated launch/block scheduling | physical-core grid-stride or supported blockification |
| Blocks below usable cores | insufficient parallelism | smaller inter-core tile or additional independent task axis |
| MTE2/MTE3 long; Vector gaps | memory or broken overlap | contiguous larger transfers, tile change, multibuffer eligibility |
| Scalar/FLOWCTRL long | address, mask, divide/modulo, dtype lowering | linearize indices, merge dimensions, hoist invariants, avoid unsupported integer ops |
| Vector long near floor | compute bound | pass fusion, libdevice, algebraic removal, explicitly approved approximation |
| UB overflow or multibuffer disabled | live set too large | shrink tile, shorten live ranges, early store, reduce widening/temp tensors |
| Tiny shapes dominated by fixed cost | launch/wrapper bound | specialization or fusion; include routing cost |

## Choose the next experiment

Write a hypothesis with one expected profiler signal. Change one primary
mechanism. Examples:

- “Reducing flattened `//`/`%` address reconstruction will reduce Scalar time.”
- “Four-row batching will reduce MTE instruction gaps while staying within the
  two-stage UB budget.”
- “Moving the invariant weight load outside the inner loop will reduce MTE2 bytes.”

If the expected signal does not change, discard the causal model even when timing
noise happens to improve.

## Stop conditions

Stop and diagnose when the same failure fingerprint repeats, measurement CV/noise
prevents a decision, or the theoretical floor leaves insufficient headroom. Do
not promise an arbitrary speedup that the measured floor cannot support.
