# Instrumenting one arm of a custom-op branch captures nothing on a build that takes the other arm, and the empty result looks like a selector problem

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-07. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Avoidance

A py_compile check proves only syntax. Before interpreting an empty capture, confirm the instrumented line is actually reachable in this build; an inline import of the probe inside a dead branch also hides missing module-level imports until runtime.

## Search terms

- operator input set missing from dump but manifest present
- enable_custom_op true instrumented fallback branch
- no tensor file written despite capture_inputs call

## Resolution

Place the input capture above the branch so it runs on either path. Confirm the live arm by evaluating enable_custom_op() in the container before assuming which kernel is under test.

## Root cause

vllm-ascend operator wrappers commonly branch on enable_custom_op() between a torch.ops._C_ascend fused kernel and a torch_npu fallback. Which arm runs depends on the build, not on the model or the request.

## Symptom

The dump manifest is produced with the expected record count from other capture points, but the operator input set is absent and no tensor payload is written. Time is then spent debugging the arming selector rather than the capture point.

## Recorded context

- vllm ascend: 0.19.1rc2.
- component: tensor-dump-instrumentation, capture-point-placement.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: capture-point-added-to-a-branch-the-build-does-not-take; first observed: 2026-09-07.
