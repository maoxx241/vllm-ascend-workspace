# Ascend Triton validation behavior contract

## Contents

- Validation configuration
- Static gate
- Case result
- Analysis semantics
- Artifact layout

## Validation configuration

```json
{
  "schema_version": 1,
  "run_id": "softmax-validation-001",
  "parent_run_id": "triton-softmax-001",
  "op_name": "softmax",
  "reference": {"kind": "torch", "path": "/src/ref.py"},
  "target": {"soc": "Ascend910B2", "cann": "...", "triton_ascend": "..."},
  "tolerances": {"float16": {"atol": 0.001, "rtol": 0.001}},
  "cases": [
    {
      "id": "fp16-128x257",
      "mode": "eager",
      "inputs": [
        {"name": "x", "shape": [128, 257], "strides": [257, 1], "dtype": "float16", "layout": "ND"}
      ],
      "scalars": {}
    }
  ]
}
```

Modes are `eager`, `compile`, and `graph`. Use only modes the candidate contract
actually promises. Every dtype in the cases requires a predeclared tolerance.

## Static gate

The AST gate checks:

- at least one `@triton.jit` function exists;
- a reachable path from `ModelNew.forward` launches a kernel;
- reachable wrapper code contains no unapproved `torch.*`, `torch.nn.functional.*`,
  or common tensor-method computation fallback.

Allocation and device-context calls are allowed. The AST gate is conservative;
manual review is still required for dynamic indirection.

## Case result

```json
{
  "schema_version": 1,
  "case_id": "fp16-128x257",
  "status": "passed",
  "comparisons": [
    {"output": "out", "max_abs": 0.0004, "max_rel": 0.0008, "cosine": 0.99999}
  ],
  "nan_match": true,
  "inf_match": true,
  "source": "/path/to/raw-result.json"
}
```

Statuses are `passed`, `numerical_mismatch`, `compilation_error`,
`runtime_error`, and `unsupported`. Every non-passed status requires an error
signature. Timing is optional diagnostic evidence and never a performance verdict.

## Analysis semantics

- `passed`: all planned cases recorded as passed;
- `failed`: at least one compile, runtime, or numerical failure;
- `inconclusive`: evidence is missing or at least one case is unsupported and no
  product failure is present.

The manifest uses run type `correctness`.

## Artifact layout

```text
validation/
├── manifest.json
├── validation-config.json
├── static-check.json
├── case-matrix.json
├── results.json
├── raw-results/
├── analysis.json
└── report.md
```
