# Ascend Triton optimization command recipes

## Plan

```bash
python -B .agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py plan \
  --output-dir .vaws-local/ascend-triton/optimization/softmax-001 \
  --config /path/to/optimization-config.json
```

## Record one round

```bash
python -B .agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py record \
  --output-dir .vaws-local/ascend-triton/optimization/softmax-001 \
  --result /path/to/normalized-round-result.json
```

Read the returned decision and `needs_diagnosis`; do not manually promote a
discarded candidate.

## Analyze

```bash
python -B .agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py analyze \
  --output-dir .vaws-local/ascend-triton/optimization/softmax-001
```

Run `analyze` when the objective is met, the round budget is exhausted, or the
workflow intentionally stops and an inconclusive terminal record is desired.
