---
name: ascend-triton-operator-development
description: Develop a first correct Ascend Triton operator from a PyTorch reference or migrate an existing GPU Triton kernel to Ascend, including semantic audit, explicit task contracts, hardware-aware grid and tiling design, implementation, and handoff to correctness validation. Use for new kernel implementation, CUDA/GPU Triton migration, or repairing a candidate that has not yet passed correctness. Do not use for a kernel that already passes all planned cases and only needs performance tuning, for isolated torch_npu or ACLNN debugging, or for model-level graph failures.
---

# Ascend Triton Operator Development

Produce a traceable candidate and prove its correctness through the validation Skill before claiming success.

## Workflow

1. Record the exact source, reference, target SoC, CANN/Triton-Ascend versions, supported shapes, dtypes, layouts, strides, scalar options, tolerances, and side effects.
2. Query `.agents/knowledge/` for target capability and known failure signatures. Treat absent facts as unknown.
3. Run `scripts/triton_development.py plan` to create the task contract and development Run Manifest.
4. Complete the generated semantic report before changing code. For GPU Triton input, audit every load, store, mask, index, grid dimension, reduction identity, atomic, and alias.
5. Write one hardware-aware sketch: logical work, physical-core mapping, tile sizes, estimated UB live set, padding semantics, and specialization boundaries.
6. Implement the smallest correct candidate. Keep the host wrapper limited to allocation, metadata extraction, dispatch, and launch; keep core computation in `@triton.jit`.
7. Use `ascend-triton-kernel-validation` on a managed remote NPU. Do not run `torch_npu` locally.
8. Run `finalize` with the candidate, completed audit, sketch, and terminal validation manifest.

## Entry point

`scripts/triton_development.py` provides:

- `plan`: validate the task config and create audit/sketch templates plus Run Manifest v1;
- `finalize`: hash and register the candidate artifacts, consume terminal correctness evidence, and generate the development report.

Read:

- [Behavior contract](references/behavior.md) for task and finalization semantics.
- [Semantic review](references/semantic-review.md) before migrating or implementing.
- [Architecture and code generation](references/architecture-and-codegen.md) while designing the kernel.
- [Command recipes](references/command-recipes.md) for controller usage.
- [Acceptance](references/acceptance.md) before claiming the first correct kernel exists.

## Rules

- Preserve the reference semantics; do not optimize away masks, padding, dtype width, or side effects without proof.
- Do not hard-code core count, UB capacity, alignment, or compiler capability across SoCs and versions.
- Keep all planned cases; never reduce a multi-shape task to the easiest case.
- Do not use GPU latency as the NPU acceptance baseline.
- A development run passes only when its linked validation manifest passes.
- Keep run state under `.vaws-local/ascend-triton/development/`.
