# Change validation command recipes

## Current worktree

```bash
python -B .agents/skills/vllm-ascend-change-validation/scripts/change_validation.py plan \
  --output-dir .vaws-local/change-validation/change-001 \
  --run-id change-validation-001 \
  --baseline HEAD \
  --candidate WORKTREE \
  --repo-root . \
  --target-repository vllm-ascend \
  --goal "Fix DCP metadata propagation"
```

## Commit range

```bash
python -B .agents/skills/vllm-ascend-change-validation/scripts/change_validation.py plan \
  --output-dir .vaws-local/change-validation/change-002 \
  --run-id change-validation-002 \
  --baseline origin/main \
  --candidate feature-branch \
  --repo-root . \
  --target-repository vllm \
  --target-repository vllm-ascend
```

## Imported cross-repository diff

```bash
python -B .agents/skills/vllm-ascend-change-validation/scripts/change_validation.py plan \
  --output-dir .vaws-local/change-validation/change-003 \
  --run-id change-validation-003 \
  --baseline recorded-baselines \
  --candidate workspace-snapshots \
  --diff-file /path/to/combined.diff \
  --target-repository vllm \
  --target-repository vllm-ascend
```

## Link evidence

Read `validation-plan.json`, then link the exact plan IDs:

```bash
python -B .agents/skills/vllm-ascend-change-validation/scripts/change_validation.py link \
  --output-dir .vaws-local/change-validation/change-001 \
  --run-manifest .vaws-local/correctness/change-001/manifest.json \
  --covers correctness-multi-rank-metadata-consistency \
  --covers correctness-eager
```

## Finalize

```bash
python -B .agents/skills/vllm-ascend-change-validation/scripts/change_validation.py finalize \
  --output-dir .vaws-local/change-validation/change-001
```

An inconclusive result is expected until every required item is covered by passed evidence.
