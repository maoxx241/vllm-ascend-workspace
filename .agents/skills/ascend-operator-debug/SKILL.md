---
name: ascend-operator-debug
description: Reduce an Ascend model-level failure to one torch_npu, ACLNN, or custom operator call, then validate explicit dtype, shape, layout, and eager/compile/graph cases against a reference implementation. Use for operator crashes, unsupported dtype or layout errors, shape-dependent numerical mismatches, or workspace API faults. Do not use for whole-model graph localization, multi-rank failures, performance benchmarking, or profiler analysis.
---

# Ascend Operator Debug

Turn a suspected operator failure into a portable reproducer and an explicit
case matrix. A model-level symptom is not an operator bug until the isolated call
reproduces it.

## Workflow

1. Capture the failing call's operator name, arguments, input metadata, execution
   mode, environment, and source stack without copying full tensors by default.
   When the arguments have to come out of a running service, use
   `ascend-tensor-dump`: `capture_inputs(stage, **named)` saves the complete
   input set including scalar and boolean options, and `assets/replay_op.py`
   feeds it to the candidate and the reference outside the service.
2. Define a trusted reference implementation and tolerances before comparing.
3. Create an explicit case matrix with `scripts/operator_debug.py plan`.
4. Run the generated cases on a remote Ascend environment. Change one dimension
   at a time: dtype, shape, layout, mode, or operator option.
5. Record each normalized result with `record`.
6. Run `analyze` to separate numerical mismatch, crash, unsupported combination,
   missing evidence, and operator-pass/integration-fail outcomes.
7. Add the smallest failing case as a regression test, then rerun the original
   model integration after the operator fix.

## Entry point

`scripts/operator_debug.py` provides:

- `plan`: validate the explicit case matrix and create the evidence layout;
- `record`: accept one result for a planned case without overwriting evidence;
- `analyze`: summarize failure axes and create a Run Manifest-linked report.

Read only the reference needed for the current phase:

- [Behavior contract](references/behavior.md)
- [Command recipes](references/command-recipes.md)
- [Acceptance](references/acceptance.md)

## Boundaries

- This skill begins after evidence identifies one operator boundary or the user
  explicitly supplies an operator reproducer.
- Whole-model eager-versus-graph localization belongs to
  `vllm-ascend-graph-debug`; hand off only after one operator call is isolated.
- Rank-dependent and collective failures belong to
  `vllm-ascend-distributed-debug`.
- Model-level throughput regressions belong to performance workflows. An optional
  operator timing value here is only supporting evidence for the isolated case.

## Rules

- Do not run `torch_npu` locally; execute operator cases on a managed remote NPU.
- Preserve exact input shape, stride, dtype, layout, device, and scalar options.
- Never silently cast inputs or relax tolerances to make a case pass.
- Pick the reference implementation deliberately: a CPU FP32 computation or the
  canonical formula. An older compatibility code path is not a golden reference;
  it can be the wrong side of the comparison.
- A service-level dump does not become a standalone reproducer on its own. The
  captured input set is the handover point between the two, and the operator
  conclusion is not complete until it explains the model-level symptom.
- Record unsupported combinations separately from product failures.
- Keep cases under `.vaws-local/operator-debug/`.
