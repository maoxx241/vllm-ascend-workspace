# Ascend Triton validation acceptance

## Static integrity

- [ ] Candidate hash is recorded.
- [ ] At least one Triton kernel exists and is reachable from `ModelNew.forward`.
- [ ] Reachable wrapper code has no PyTorch computation fallback.
- [ ] Manual review covers dynamic calls the AST gate cannot prove.

## Matrix and execution

- [ ] Reference, tolerance, environment, and every case are explicit.
- [ ] Boundary shapes, tails, dtypes, layouts, strides, and scalar branches are covered.
- [ ] Every case ran on the same intended Ascend environment.
- [ ] No implicit cast, contiguous copy, or case deletion changed the contract.

## Result integrity

- [ ] Output structure, shape, dtype, NaN/Inf behavior, and values were compared.
- [ ] Raw evidence links exist for every failure.
- [ ] Passed count comes from validation, not benchmark process success.
- [ ] `passed_cases == total_cases > 0` before any performance workflow begins.
