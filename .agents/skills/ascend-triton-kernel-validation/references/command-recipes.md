# Ascend Triton validation command recipes

## Plan and static gate

```bash
python -B .agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py plan \
  --output-dir .vaws-local/ascend-triton/validation/softmax-001 \
  --config /path/to/validation-config.json \
  --kernel /path/to/softmax_ascend.py
```

Inspect `static-check.json` before remote execution.

## Record one remote result

```bash
python -B .agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py record \
  --output-dir .vaws-local/ascend-triton/validation/softmax-001 \
  --result /path/to/normalized-case-result.json
```

## Analyze

```bash
python -B .agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py analyze \
  --output-dir .vaws-local/ascend-triton/validation/softmax-001
```

Consume `analysis.json` and `manifest.json`, not mixed runtime stdout.
