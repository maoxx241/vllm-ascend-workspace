---
name: ascend-triton-workflow
description: Coordinate an end-to-end Ascend Triton operator task across development, correctness validation, optimization, and linked evidence.
---

# Ascend Triton Workflow

Coordinate the lifecycle without duplicating the implementation owned by the stage Skills.

## Workflow

1. Freeze the source, operator contract, target SoC, software versions, case set, and requested performance objective.
2. Query `.agents/knowledge/` for relevant capability, validation, and failure-signature facts. Treat missing facts as unknown.
3. Run `scripts/triton_workflow.py plan` to create the stage plan and parent Run Manifest.
4. Execute required stages with their owners:
   - first correct kernel or GPU migration: `ascend-triton-operator-development`;
   - compile and full-case correctness gate: `ascend-triton-kernel-validation`;
   - single-kernel profiling and performance iteration: `ascend-triton-kernel-optimization`.
5. Before remote execution, establish `remote-code-parity`; run `torch_npu` and Triton only in a managed Ascend environment.
6. Link every child Run Manifest to its planned stage with `link`.
7. Run `finalize` and deliver the workflow report with missing, failed, and untested coverage explicit.

## Entry point

`scripts/triton_workflow.py` provides:

- `plan`: validate the workflow config, create ordered stage items, and initialize a parent Run Manifest;
- `link`: bind one terminal child Run Manifest to one stage without overwriting prior evidence;
- `finalize`: aggregate required-stage evidence and produce `workflow-report.md`.

Read:

- [Behavior contract](references/behavior.md) for config, stage, link, and status semantics.
- [Command recipes](references/command-recipes.md) for the complete lifecycle.
- [Acceptance](references/acceptance.md) before claiming an operator workflow complete.

## Rules

- Keep stage ownership strict; do not implement migration, validation, or optimization inside this Skill.
- Never let performance evidence substitute for correctness evidence.
- A passed workflow requires every required stage to have a passed terminal child manifest.
- A failed child makes the workflow failed; missing, unsupported, cancelled, or inconclusive required evidence makes it inconclusive.
- Record GPU timing only as cross-platform context; use a comparable NPU baseline for NPU optimization acceptance.
- Keep orchestration state under `.vaws-local/ascend-triton/workflows/`.
