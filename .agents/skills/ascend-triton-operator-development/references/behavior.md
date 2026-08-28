# Ascend Triton development behavior contract

## Contents

- Task configuration
- Development artifacts
- Validation handoff
- Final status

## Task configuration

```json
{
  "schema_version": 1,
  "run_id": "softmax-development-001",
  "parent_run_id": "triton-softmax-001",
  "op_name": "softmax",
  "mode": "gpu-migration",
  "source": {"kind": "gpu-triton", "path": "/src/softmax.py"},
  "reference": {"kind": "torch", "path": "/src/softmax_ref.py"},
  "target": {"soc": "Ascend910B2", "cann": "...", "triton_ascend": "..."},
  "tolerances": {"float16": {"atol": 0.001, "rtol": 0.001}},
  "cases": [
    {
      "id": "fp16-128x257",
      "inputs": [
        {"name": "x", "shape": [128, 257], "dtype": "float16", "layout": "ND", "strides": [257, 1]}
      ],
      "scalars": {}
    }
  ]
}
```

`mode` is `direct` or `gpu-migration`. Every case has a stable ID and explicit
input metadata. The config records the supported contract, not just the cases
that happen to pass.

## Development artifacts

`plan` creates:

- `task-config.json`: immutable input contract;
- `task-contract.json`: compact operator/case handoff;
- `semantic-report.md`: mandatory audit template;
- `sketch.md`: mandatory design template;
- `candidates/` and `artifacts/` directories;
- `manifest.json`: Run Manifest v1 with run type `debug`.

The semantic report and sketch must be completed, not left with unchecked
placeholders. The controller rejects empty files but cannot judge design quality;
use the acceptance checklist.

## Validation handoff

`finalize` accepts a terminal `correctness` Run Manifest. Its `parent_run_id`
must point either to this development run or to the same parent workflow. This
supports standalone development and sibling stages under one workflow.

The candidate source, semantic report, and sketch are hashed into the manifest.
The validation manifest path and status are recorded in `development-result.json`.

## Final status

- validation `passed` -> development `passed`;
- validation `failed` -> development `failed`;
- validation `inconclusive` or `cancelled` -> development `inconclusive`.

No validation manifest means the run remains unfinished; `finalize` rejects it.
