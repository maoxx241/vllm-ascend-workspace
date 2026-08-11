# Ascend Triton semantic review

## Contents

- Operator contract
- Loads and stores
- Masks and padding
- Index and grid mapping
- Numerical and side-effect semantics
- Migration checklist

## Operator contract

Record input/output shapes, dtypes, layouts, strides, scalar parameters, aliases,
in-place behavior, atomics, randomness, and supported dynamic ranges. Separate
what the source currently assumes from what the public operator contract promises.

## Loads and stores

For every `tl.load`, record:

- pointer expression and source tensor;
- logical lane shape;
- input-validity mask;
- `other` or padding identity;
- whether an invalid lane reaches reduce, divide, exp, compare, index, or store.

For every `tl.store`, record the output-validity mask independently. A load mask
answers which input addresses are readable; a store mask answers which output
positions must be written. Reusing one for the other can silently skip valid
outputs.

## Masks and padding

- Use bitwise tensor operators (`&`, `|`) rather than Python boolean operators.
- Preserve mathematical identities: sum uses zero, max commonly uses negative
  infinity, and min commonly uses positive infinity.
- Treat `care_padding=False` as an optimization that requires proof that padded
  lanes are dead or overwritten before use.
- Revalidate tail blocks, fully masked blocks, tiny shapes, NaN, and Inf whenever
  padding behavior changes.

## Index and grid mapping

Classify each address as contiguous, regular-strided, or data-dependent discrete.
Map every GPU `program_id` dimension to one logical task. If dimensions are
flattened for Ascend, record the reconstruction formula and the added scalar
division/modulo cost.

Do not automatically flatten a legal regular 2D mapping. Do not automatically
preserve a very large GPU logical grid. Design and measure both physical-core
grid-stride and compiler-assisted blockification candidates when supported.

## Numerical and side-effect semantics

- Record accumulation dtype and reduction order.
- Record math approximations separately from architecture migration.
- Preserve repeated-index and atomic semantics.
- Preserve output initialization and untouched-output behavior.
- Never relax tolerance after observing a failure without a documented numerical
  justification.

## Migration checklist

- [ ] GPU-only host APIs and device contexts are identified.
- [ ] Triton language features are checked against the target version.
- [ ] Grid semantics are mapped, not merely renamed.
- [ ] Every mask and padding identity is explained.
- [ ] All dynamic shapes and scalar specializations remain represented.
- [ ] External dependencies and hidden fallbacks are removed or made explicit.
- [ ] The source is left intact; the Ascend candidate is a separate artifact.
