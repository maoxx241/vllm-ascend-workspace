# Correctness validation acceptance

## Reproducibility

- [ ] Baseline and candidate use the same case document.
- [ ] Workspace snapshots, environment, model, weights, tokenizer, topology, and feature flags are recorded.
- [ ] Exact-comparison cases use `temperature=0` and an explicit seed.
- [ ] Commands or equivalent executor configurations are recorded.

## Execution

- [ ] Code parity was established immediately before each remote execution.
- [ ] Offline or online executor produced a normalized result file.
- [ ] Offline execution and its Python subprocesses resolve the materialized packages rather than the outer `vllm/` repository namespace.
- [ ] Service health was confirmed before online cases.
- [ ] Repeated outputs are stable or the case is classified flaky.
- [ ] Infrastructure and unsupported failures are not counted as product regressions.

## Comparison

- [ ] Every planned case has baseline and candidate evidence.
- [ ] Both results carry an `execution` block, and every difference between them was declared at `init` with `--allowed-difference`; `compare` refuses undeclared differences.
- [ ] `compare` consumed a comparable observational certificate (`comparability-certificate.json`); empty, null, or whitespace-only identity, declared-only must-observe keys, or a declaration/observation mismatch cannot pass.
- [ ] Matching empty answers are not treated as `exact_match`.
- [ ] A verdict on a run with declared differences is read as attributable to the declared variable, not to the code change.
- [ ] Token or text comparison is used where exactness is meaningful.
- [ ] Numeric tolerances are explicit and justified.
- [ ] Task metric direction and regression thresholds are explicit.
- [ ] Eager/graph divergence routes to graph debug.

## Delivery

- [ ] `manifest.json`, `cases.json`, `execution.json`, `comparison.json`, `report.md`, raw outputs, and `reproduction.sh` exist.
- [ ] Run Manifest v1 validates and links every delivered artifact.
- [ ] The report lists unsupported and untested combinations.
- [ ] A passed run contains only exact or within-tolerance cases.
