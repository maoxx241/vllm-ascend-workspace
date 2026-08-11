# Ascend Triton architecture and code generation

## Contents

- Capability snapshot
- Grid and physical cores
- UB and tiling
- Pipeline and dtype
- Code-generation rules

## Capability snapshot

Before design, record the exact SoC, Vector/Cube core counts, CANN, driver,
`torch_npu`, and `triton-ascend` versions. Query runtime/device properties where
available. Mark UB, alignment, or compiler options unknown rather than copying a
different SoC's constants.

## Grid and physical cores

Ascend grid selection is a physical resource mapping decision. Evaluate:

1. physical-core-scale grid plus a grid-stride loop;
2. a larger logical grid with supported blockification/compiler options;
3. a legal multidimensional mapping when it avoids expensive flattening.

Balance per-core iterations and keep front/tail work close. Count scalar
division, modulo, branch, and address-generation work introduced by task
reconstruction.

## UB and tiling

Budget the peak live set, not total input size:

```text
usable_per_stage = floor(UB_bytes * safety_factor / pipeline_stages)
tiles_per_iteration <= usable_per_stage / peak_live_bytes_per_tile
```

Include simultaneous loads, widened fp32 temporaries, accumulators, masks,
indices, slice/broadcast temporaries, and values still live at store. A fixed
85 KB budget is only a 192 KB/double-buffer heuristic, not a portable contract.

Start with a conservative correct tile. Optimization owns later tile expansion.

## Pipeline and dtype

Design address generation, GM-to-UB loads, Vector computation, and UB-to-GM
stores so later optimization can overlap them. Keep iteration addresses
independent when possible. Avoid unnecessary int64 arithmetic and unsupported
integer vector operations, but do not cast large indices to fp32 when exactness
would be lost.

## Code-generation rules

- Keep core computation inside one or more `@triton.jit` kernels.
- Limit the host wrapper to allocation, metadata, safe dispatch, and launch.
- Do not call PyTorch tensor computation as a fallback for unsupported cases.
- Keep mask and identity semantics explicit in the first candidate.
- Use `tl.constexpr` only for bounded specialization dimensions and operations
  that require compile-time values.
- Preserve a generic fallback when adding shape-specialized kernels.
- Write complete code with no placeholders before handing off to validation.
