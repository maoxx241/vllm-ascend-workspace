# Ascend operator debug command recipes

## Plan

```bash
python -B .agents/skills/ascend-operator-debug/scripts/operator_debug.py plan \
  --output-dir .vaws-local/operator-debug/case-001 \
  --config /path/to/operator-config.json
```

## Execute remotely

Use the generated matrix to run one operator invocation per case on an Ascend
NPU. Store raw stdout, stderr, stack traces, and input metadata under the case
directory. Do not run `torch_npu` on the local machine.

## Record

```bash
python -B .agents/skills/ascend-operator-debug/scripts/operator_debug.py record \
  --output-dir .vaws-local/operator-debug/case-001 \
  --result /path/to/normalized-case-result.json
```

Each case can be recorded once, so corrected evidence requires a new case ID or a
new run.

## Analyze

```bash
python -B .agents/skills/ascend-operator-debug/scripts/operator_debug.py analyze \
  --output-dir .vaws-local/operator-debug/case-001
```
