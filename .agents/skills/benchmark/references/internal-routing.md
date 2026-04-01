# Internal Routing: Benchmark

Public contract: `../SKILL.md`

Use this file only after the public `benchmark` contract has been selected.

## Internal Delegation

### Public Action -> Internal Contract

- `benchmark` -> `branch-context`, `code-parity`, `runtime-environment`, `benchmark`

### Internal Contract -> Backend

- `branch-context` -> current repo target resolution in `tools/lib/repo_targets.py::resolve_repo_targets()`
- `code-parity` -> `tools/lib/code_parity.py::verify_code_parity()` and `tools/lib/code_parity.py::ensure_code_parity()`
- `runtime-environment` -> `tools/lib/runtime_env.py::ensure_runtime_environment()`
- `benchmark` -> `tools/lib/benchmark.py::get_benchmark_preset()` and `tools/lib/benchmark.py::run_benchmark_preset()`

## Action Routing

- `run qwen3 35b tp4 benchmark`
  - internal contracts: `branch-context`, `code-parity`, `runtime-environment`, `benchmark`
  - backend entrypoints: `tools/lib/benchmark.py::run_benchmark_preset()` after code parity and runtime environment checks
- `inspect the benchmark preset`
  - internal contracts: `benchmark`
  - backend entrypoints: `tools/lib/benchmark.py::get_benchmark_preset()`

## Internal State Touched

- `.workspace.local/state.json`
- `benchmarking/`

## Related Tests

- `tests/test_vaws_benchmark.py`
- `tests/test_vaws_acceptance.py`
