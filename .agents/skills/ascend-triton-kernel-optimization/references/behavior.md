# Ascend Triton optimization behavior contract

## Contents

- Optimization configuration
- Baseline contract
- Round result
- Decision semantics
- Final status

## Optimization configuration

```json
{
  "schema_version": 1,
  "run_id": "softmax-optimization-001",
  "parent_run_id": "triton-softmax-001",
  "op_name": "softmax",
  "kernel": {"path": "/src/softmax_ascend.py"},
  "validation_manifest": "/runs/validation/manifest.json",
  "target": {"soc": "Ascend910B2", "cann": "...", "triton_ascend": "..."},
  "cases": [
    {"id": "fp16-128x257", "weight": 1.0}
  ],
  "baseline": [
    {"case_id": "fp16-128x257", "median_us": 18.2, "repeats": 50, "source": "/raw/baseline.json"}
  ],
  "objective": {
    "target_relative_improvement": 0.10,
    "min_relative_improvement": 0.01,
    "noise_floor": 0.005,
    "max_case_regression": 0.02
  },
  "max_rounds": 20,
  "max_consecutive_failures": 3
}
```

The validation manifest must be passed, use run type `correctness`, and cover the
starting kernel. Baseline measurements must cover every case exactly once.

## Baseline contract

- use device time for the target kernel and separately record wrapper latency;
- exclude compile and warmup;
- preserve raw measurements and profiler artifacts;
- use the same environment, workload, case definitions, and measurement policy
  for every candidate;
- use a comparable NPU implementation as the acceptance baseline.

## Round result

```json
{
  "schema_version": 1,
  "round": 1,
  "parent_kernel_sha256": "64-lowercase-hex",
  "candidate": {"path": "/src/candidate.py"},
  "hypothesis": "Batch four contiguous rows to reduce MTE instruction overhead",
  "change": "ROWS_PER_ITER=1 -> 4",
  "verification": {"manifest": "/runs/round-1-validation/manifest.json"},
  "measurements": [
    {"case_id": "fp16-128x257", "median_us": 15.4, "repeats": 50, "source": "/raw/round-1.json"}
  ],
  "profiler_signals": {"aiv_scalar_ratio": 0.12, "aiv_vec_ratio": 0.54}
}
```

Rounds are sequential and their parent hash must equal the current best hash.
The verification manifest must be passed and refer to this optimization run or
its parent workflow. Failed verification may omit measurements and becomes `FAIL`.

## Decision semantics

- `KEEP`: weighted relative improvement meets `min_relative_improvement`, exceeds
  noise, and no case exceeds `max_case_regression`;
- `NOISE`: absolute weighted improvement is below `noise_floor`;
- `DISCARD`: correct candidate does not meet keep conditions;
- `FAIL`: validation is not passed.

Only KEEP updates the best kernel and best per-case measurements. The controller
also reports improvement from the original baseline and whether the target is met.

## Final status

- `passed`: target relative improvement is met;
- `failed`: target is unmet and round budget is exhausted or diagnosis threshold
  is reached;
- `inconclusive`: target is unmet but additional planned rounds remain.

The manifest uses run type `performance`.
