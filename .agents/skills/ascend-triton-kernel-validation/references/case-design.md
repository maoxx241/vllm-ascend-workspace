# Ascend Triton validation case design

## Contents

- Contract-driven matrix
- Boundary shapes
- Layout and indexing
- Numerical distributions
- Tolerances and comparisons

## Contract-driven matrix

Select the minimum matrix that covers every implementation branch and public
promise. Do not execute an uncontrolled Cartesian product, but do not omit a
dimension merely because it is expensive.

Change one meaningful axis per diagnostic case when localizing a failure:
shape, dtype, layout/stride, execution mode, or scalar option.

## Boundary shapes

Include as applicable:

- minimum, representative, and maximum supported sizes;
- power-of-two and non-power-of-two sizes;
- aligned and non-aligned tails;
- empty or singleton dimensions when legal;
- values around tile, block, reduction, and specialization boundaries;
- enough tasks to exercise fewer-than-core, near-core, and more-than-core grids.

## Layout and indexing

Record exact strides and layout. If arbitrary strides are supported, test them
without silently calling `.contiguous()`. For gather/index/atomic kernels include
negative/invalid cases when the contract defines them, repeated indices, and
data-dependent discrete access.

## Numerical distributions

Include zeros, signs, extrema, near-overflow and near-underflow values, NaN/Inf
when defined, and distributions that stress reduction order. Validate padding
identity with fully or partially masked lanes.

## Tolerances and comparisons

- Fix `atol` and `rtol` before observing candidate results.
- Compare output structure, shape, dtype, NaN/Inf locations, then numeric values.
- Use exact comparison for integer and boolean outputs unless semantics state otherwise.
- Use fp32 accumulation expectations where the algorithm requires it.
- Record max absolute, max relative, and a stable similarity metric when useful;
  none replaces elementwise acceptance by itself.
