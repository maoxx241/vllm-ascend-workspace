# Internal Routing: Benchmark

Public contract: `../SKILL.md`

This file is a maintainer backstop. The public `SKILL.md` is the normal execution surface, and agents should not need this file to understand the default public recipe.

## Sanctioned Adapter Surface

- Ordinary execution surface: `tools/vaws.py benchmark run`
- Service inspection companion surface: `tools/vaws.py serving list` and `tools/vaws.py serving status`
- Do not route a normal agent directly to `tools/lib/*.py`; library modules remain implementation owners behind the sanctioned adapter surface.

## Internal Delegation

### Public Action -> Internal Contract

- `benchmark` -> `branch-context`, `code-parity`, `runtime-environment`, `serving-lifecycle`, `benchmark-execution`

### Internal Contract -> Implementation Owner

- `branch-context` -> implemented by `tools/lib/branch_context.py`
- `code-parity` -> implemented by `tools/lib/code_parity.py`
- `runtime-environment` -> implemented by `tools/lib/runtime_env.py`
- `serving-lifecycle` -> discovery family `.agents/discovery/families/serving-lifecycle.yaml`
- `benchmark-execution` -> discovery family `.agents/discovery/families/benchmark-execution.yaml`
- `benchmark-session-cache` -> implemented by `tools/lib/serving_session.py`
- `benchmark-helper` -> implemented by `tools/lib/benchmark_execution.py`
- `benchmark-compatibility-wrapper` -> implemented by `tools/lib/vaws_benchmark.py`

## Action Routing

- `run benchmark against the active service session`
  - sanctioned adapter: `tools/vaws.py benchmark run`
  - family manifests: `.agents/discovery/families/benchmark-execution.yaml`, `.agents/discovery/families/serving-lifecycle.yaml`
  - explicit-service path: `serving.list_sessions` or `serving.describe_session` -> `benchmark.run_probe`
  - internal contracts: `branch-context`, `code-parity`, `runtime-environment`, `serving-lifecycle`, `benchmark-execution`
- `inspect the benchmark preset`
  - sanctioned adapter: `tools/vaws.py benchmark run`
  - family manifest: `.agents/discovery/families/benchmark-execution.yaml`
  - atomic tools: `benchmark.describe_preset`, `benchmark.describe_run`
  - internal contracts: `benchmark-execution`

## Internal State Touched

- `.workspace.local/benchmark-runs/`
- remote service manifests under `/vllm-workspace/artifacts/services/`

## Related Tests

- `tests/test_vaws_benchmark.py`
- `tests/test_workspace_skill_contracts.py`
- `tests/test_workspace_skill_routing_contracts.py`
