# Ascend Triton development command recipes

## Plan

```bash
python -B .agents/skills/ascend-triton-operator-development/scripts/triton_development.py plan \
  --output-dir .vaws-local/ascend-triton/development/softmax-001 \
  --config /path/to/development-config.json
```

Complete `semantic-report.md` and `sketch.md`, then place candidates under the
generated `candidates/` directory.

## Validate remotely

Use `ascend-triton-kernel-validation` with the same cases and tolerances. Set the
validation run's `parent_run_id` to this development run or the shared workflow.

## Finalize

```bash
python -B .agents/skills/ascend-triton-operator-development/scripts/triton_development.py finalize \
  --output-dir .vaws-local/ascend-triton/development/softmax-001 \
  --kernel /path/to/softmax_ascend.py \
  --semantic-report .vaws-local/ascend-triton/development/softmax-001/semantic-report.md \
  --sketch .vaws-local/ascend-triton/development/softmax-001/sketch.md \
  --validation-manifest /path/to/validation/manifest.json
```
