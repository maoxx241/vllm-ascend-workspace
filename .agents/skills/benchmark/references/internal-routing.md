# Internal Routing: Benchmark

Public contract: `../SKILL.md`

Use this file only after the public `benchmark` contract has been selected.

## Sanctioned Adapter Surface

- Ordinary execution surface: `tools/vaws.py benchmark run`
- Compatibility-only adapter: `tools/vaws.py internal acceptance run` may consume ready capabilities during migration, but it is not the public benchmark surface.
- Do not route a normal agent directly to `tools/lib/*.py`; library modules remain implementation owners behind the sanctioned adapter surface.

## Internal Delegation

### Public Action -> Internal Contract

- `benchmark` -> `branch-context`, `code-parity`, `runtime-environment`, `benchmark`

### Internal Contract -> Implementation Owner

- `branch-context` -> implemented by `tools/lib/branch_context.py`
- `code-parity` -> implemented by `tools/lib/code_parity.py`
- `runtime-environment` -> implemented by `tools/lib/runtime_env.py`
- `benchmark` -> implemented by `tools/lib/benchmark.py`

## Action Routing

- `run qwen3 35b tp4 benchmark`
  - sanctioned adapter: `tools/vaws.py benchmark run`
  - internal contracts: `branch-context`, `code-parity`, `runtime-environment`, `benchmark`
- `inspect the benchmark preset`
  - sanctioned adapter: `tools/vaws.py benchmark run`
  - internal contracts: `benchmark`

## Internal State Touched

- `.workspace.local/state.json`
- `benchmarking/`

## Related Tests

- `tests/test_vaws_benchmark.py`
- `tests/test_vaws_acceptance.py`
- `tests/test_workspace_skill_contracts.py`
- `tests/test_workspace_skill_routing_contracts.py`
