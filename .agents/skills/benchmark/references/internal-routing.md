# Internal Routing: Benchmark

Public contract: `../SKILL.md`

Use this file only after the public `benchmark` contract has been selected.

## Sanctioned Adapter Surface

- Ordinary execution surface: `tools/vaws.py benchmark run`
- Do not route a normal agent directly to `tools/lib/*.py`; library modules remain implementation owners behind the sanctioned adapter surface.

## Internal Delegation

### Public Action -> Internal Contract

- `benchmark` -> `branch-context`, `code-parity`, `runtime-environment`, `serving-session`, `benchmark`

### Internal Contract -> Implementation Owner

- `branch-context` -> implemented by `tools/lib/branch_context.py`
- `code-parity` -> implemented by `tools/lib/code_parity.py`
- `runtime-environment` -> implemented by `tools/lib/runtime_env.py`
- `serving-session` -> implemented by `tools/lib/serving.py`
- `benchmark` -> implemented by `tools/lib/benchmark.py`

## Action Routing

- `run benchmark against the active service session`
  - sanctioned adapter: `tools/vaws.py benchmark run`
  - internal contracts: `branch-context`, `code-parity`, `runtime-environment`, `serving-session`, `benchmark`
- `inspect the benchmark preset`
  - sanctioned adapter: `tools/vaws.py benchmark run`
  - internal contracts: `benchmark`

## Internal State Touched

- `.workspace.local/state.json`
- `benchmarking/`
- `serving/`

## Related Tests

- `tests/test_vaws_benchmark.py`
- `tests/test_workspace_skill_contracts.py`
- `tests/test_workspace_skill_routing_contracts.py`
