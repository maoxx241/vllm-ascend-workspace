# Saving only contiguous host copies of tensors erases the stride and storage pointer evidence that cache-aliasing defects leave behind

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-07. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Avoidance

For any suspected cache, block-table or state-reuse defect, compare storage pointers, offsets and strides before comparing max_abs. A conclusion of the form 'layer N is numerically off' is not a root cause; 'writer, address, victim reader' is.

## Search terms

- different stride shared storage kv group
- contiguous cpu dump loses stride pointer
- cache collision invisible in tensor comparison

## Resolution

Record logical shape and dtype alongside physical stride, storage_offset, storage pointer and NPU format for every captured tensor, and group stages by shared storage pointer before comparing values. ascend-tensor-dump writes these into every manifest record and reports storage_aliases with a stride_conflict flag.

## Root cause

The defect is structural rather than arithmetic: two consumers share one device allocation through different compact strides or offsets, so one writer corrupts another reader. contiguous() and .cpu() normalize exactly the layout information that identifies this.

## Symptom

Numerical comparison shows a layer producing wrong values but no comparison explains why, because every dumped tensor is a clean contiguous array. Investigation stalls on 'this layer is numerically off' without reaching a cause.

## Recorded context

- soc: A3.
- topology: multinode.
- component: tensor-dump-instrumentation, dump-metadata.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: contiguous-cpu-save-erases-storage-aliasing-evidence; first observed: 2026-09-07.
