# Ascend Triton optimization techniques

## Contents

- Grid and load balance
- UB live set and multibuffer
- MTE, Vector, and Scalar overlap
- Masks and padding
- Dtype and scalar lowering
- Memory access
- Specialization, fusion, and autotune

## Grid and load balance

Treat grid as physical resource mapping. Benchmark physical-core-scale
grid-stride scheduling against compiler-supported blockification. Balance
front/tail work and account for scalar division/modulo introduced by flattening.
A 1D grid is a strong candidate, not a universal rule.

## UB live set and multibuffer

Budget peak simultaneous live bytes, including loads, widened temporaries,
accumulators, masks, indices, slices, and values pending store:

```text
usable_per_stage = floor(UB_bytes * safety_factor / pipeline_stages)
tile_count <= usable_per_stage / peak_live_bytes_per_tile
```

For a 192 KB UB, 85 KB is only a conservative two-stage heuristic. Query the
target. Shorten live ranges, compute/store completed streams early, and avoid
loading every input before any computation.

## MTE, Vector, and Scalar overlap

- Make iteration addresses independent (`base + iteration * stride`).
- Interleave load, compute, and store in tiles.
- Reorder independent loads to issue early without inflating the live set.
- Hoist loop-invariant loads and address terms.
- Ensure enough looped transfers exist for multibuffer to matter.

## Masks and padding

Default padding or `other` can introduce Vector fill dependencies. Try
`care_padding=False` only after proving invalid lanes are dead or overwritten.
Preserve reduction identities. Keep load and store masks semantically separate.

## Dtype and scalar lowering

Profile int64 arithmetic, integer compare/reduce, discrete masks, and complex
divide/modulo. Use int32 only when ranges are safe. Do not cast large exact
indices to fp32. Verify the backend actually lowers the revised code to Vector
operations.

## Memory access

- Prefer contiguous aligned multi-row transfers.
- Construct regular indices with `tl.arange` instead of reading computable tables.
- Handle data-dependent gather/index access in continuous inner segments; large
  discrete 2D masks may lower to scalar loops.
- Include `.contiguous()` copies in end-to-end timing unless contiguity is part of
  the public input contract.

## Specialization, fusion, and autotune

- Use `tl.constexpr` for bounded specialization, not every dynamic length.
- Fuse passes when saved GM traffic exceeds new UB/synchronization/compile cost.
- Split or specialize when fusion breaks UB/pipeline behavior or shapes need
  different algorithms; retain a generic fallback and measure routing.
- Tune BLOCK/tile/grid plus supported Ascend compiler options such as
  `multibuffer`, `unit_flag`, and `auto_blockify_size` only when present in the
  recorded software version.
