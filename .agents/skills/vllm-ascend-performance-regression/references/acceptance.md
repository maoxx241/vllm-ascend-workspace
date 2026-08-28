# Performance regression acceptance

## Parity

- [ ] Baseline and candidate use distinct worktrees and sessions.
- [ ] Machine policy, devices, model and weight hash, environment, topology, Serving arguments, Benchmark arguments, dataset, request rate, and concurrency are identical.
- [ ] Every measurement uses the planned config hash.

## Execution

- [ ] Warmups ran for both states and are excluded.
- [ ] Measurements followed the alternating schedule exactly.
- [ ] Raw Benchmark output was normalized by the controller rather than manually rewritten.
- [ ] Each result links to its raw Benchmark artifact.
- [ ] Temperature, background load, and cache drift were considered.

## Analysis

- [ ] Every threshold declares higher-is-better or lower-is-better.
- [ ] Raw values, mean, sample deviation, CV, and outlier indices are reported.
- [ ] Outlier exclusion policy was fixed before analysis.
- [ ] No metric exceeds `max_cv` for a pass or fail conclusion.
- [ ] Missing or insufficient values yield inconclusive.

## Delivery

- [ ] Config, parity check, schedule, measurements, comparison, report, reproduction, and Run Manifest exist.
- [ ] A profiling recommendation is explicit and does not imply profiling was already collected.
