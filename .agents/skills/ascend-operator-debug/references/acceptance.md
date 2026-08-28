# Ascend operator debug acceptance

## Reproduction

- [ ] The failure reproduces with one operator invocation.
- [ ] Input shape, stride, dtype, layout, device, and attributes are explicit.
- [ ] The reference implementation is independent enough to detect the defect.
- [ ] Tolerances were fixed before examining candidate output.

## Matrix

- [ ] Each case changes one meaningful dimension.
- [ ] Eager, compile, or graph mode is explicit.
- [ ] Unsupported cases are not counted as product failures.
- [ ] Crash signatures and numerical comparisons retain raw evidence links.

## Fix validation

- [ ] The smallest failing case becomes a regression test when practical.
- [ ] The isolated case passes after the fix.
- [ ] Nearby dtype, shape, and layout cases still pass.
- [ ] The original model integration passes after the fix.
- [ ] Temporary tensor dumps and instrumentation are removed or documented.
