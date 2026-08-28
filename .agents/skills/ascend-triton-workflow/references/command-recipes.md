# Ascend Triton workflow command recipes

## Plan

```bash
python -B .agents/skills/ascend-triton-workflow/scripts/triton_workflow.py plan \
  --output-dir .vaws-local/ascend-triton/workflows/softmax-001 \
  --config /path/to/workflow-config.json
```

Read `stage-plan.json` and execute only the next stage with its owning Skill.

## Link one child

```bash
python -B .agents/skills/ascend-triton-workflow/scripts/triton_workflow.py link \
  --output-dir .vaws-local/ascend-triton/workflows/softmax-001 \
  --stage validation \
  --manifest /path/to/validation/manifest.json
```

The child manifest must already be terminal and must name this workflow as its
parent.

## Finalize

```bash
python -B .agents/skills/ascend-triton-workflow/scripts/triton_workflow.py finalize \
  --output-dir .vaws-local/ascend-triton/workflows/softmax-001
```

Inspect `workflow-summary.json` and `workflow-report.md`. Do not infer completion
from console output alone.
