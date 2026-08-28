---
name: ascend-triton-kernel-validation
description: Validate one Ascend Triton kernel against a trusted reference across an explicit shape, dtype, layout, stride, scalar-option, and execution-mode case matrix, with static detection of missing kernel launches and PyTorch computation fallback plus Run Manifest evidence. Use before any performance claim, after migration or implementation changes, or for shape-dependent compile/runtime/numerical failures in a Triton candidate. Do not use to generate the kernel, optimize an already-correct kernel, diagnose a non-Triton torch_npu or ACLNN call, or localize a whole-model graph failure.
---

# Ascend Triton Kernel Validation

Turn a candidate into explicit compile and correctness evidence. A successful process exit or benchmark run is not correctness proof.

## Workflow

1. Freeze the candidate hash, trusted reference, predeclared tolerances, target environment, and explicit case matrix.
2. Query `.agents/knowledge/` with any observed compile, runtime, or numerical signature before repeating diagnosis.
3. Run `scripts/triton_validation.py plan`. It invokes `validate_triton_impl.py` and rejects missing Triton kernels, a `ModelNew.forward` path that does not launch them, or reachable PyTorch tensor computation fallback.
4. Before remote execution, establish `remote-code-parity`. Run every planned case on a managed Ascend NPU; do not run `torch_npu` locally.
5. Compare shape and dtype first, then NaN/Inf behavior and numeric values. Preserve raw stdout, stderr, stack, and comparison artifacts.
6. Normalize one result per case and run `record`. Do not overwrite evidence.
7. Run `analyze`. Only `passed_cases == total_cases > 0` produces a passed manifest.
8. Hand the passed manifest to development or optimization. Do not benchmark a failed or inconclusive candidate.

## Entry points

- `scripts/validate_triton_impl.py`: AST-only fallback and launch gate.
- `scripts/triton_validation.py`:
  - `plan`: validate config and candidate, create case matrix and Run Manifest v1;
  - `record`: accept one normalized case result;
  - `analyze`: classify the full matrix and generate the report.

Read:

- [Behavior contract](references/behavior.md) for config, result, and status schemas.
- [Case design](references/case-design.md) before choosing the matrix and tolerances.
- [Command recipes](references/command-recipes.md) for the lifecycle.
- [Acceptance](references/acceptance.md) before declaring correctness.

## Rules

- Keep compile error, runtime error, numerical mismatch, unsupported, and missing evidence distinct.
- Never silently cast, make inputs contiguous, remove difficult shapes, or relax tolerance to pass.
- Load masks protect readable input addresses; store masks protect writable output positions.
- Treat tail blocks, fully masked blocks, non-power-of-two shapes, and dynamic specialization boundaries as first-class cases.
- Keep run state under `.vaws-local/ascend-triton/validation/`.
