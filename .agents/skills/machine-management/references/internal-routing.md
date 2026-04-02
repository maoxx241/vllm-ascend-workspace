# Internal Routing: Machine Management

Public contract: `../SKILL.md`

Use this file only after the public `machine-management` contract has been selected.

## Sanctioned Adapter Surface

- Ordinary execution surface: `tools/vaws.py machine add`, `tools/vaws.py machine verify`, `tools/vaws.py machine list`, and `tools/vaws.py machine remove`
- Diagnostic companion surface: `tools/vaws.py doctor`
- Do not route a normal agent directly to `tools/lib/*.py`; library modules remain implementation owners behind the sanctioned adapter surface.

## Internal Delegation

### Public Action -> Internal Contract

- `machine-management` -> `machine-runtime`, `storage-root`, `code-parity`, `runtime-environment`

### Internal Contract -> Implementation Owner

- `machine-runtime` -> implemented by `tools/lib/machine.py` and `tools/lib/remote.py`
- `code-parity` -> implemented by `tools/lib/code_parity.py`
- `runtime-environment` -> implemented by `tools/lib/runtime_env.py`

## Action Routing

- `attach this machine`
  - sanctioned adapter: `tools/vaws.py machine add`
  - internal contracts: `machine-runtime`
- `check whether this machine is ready`
  - sanctioned adapter: `tools/vaws.py machine verify`
  - internal contracts: `machine-runtime`, `code-parity`, `runtime-environment`
- `repair machine readiness without touching service lifecycle`
  - sanctioned adapter: `tools/vaws.py machine verify`
  - internal contracts: `machine-runtime`, `code-parity`, `runtime-environment`
- `list attached machines`
  - sanctioned adapter: `tools/vaws.py machine list`
  - internal contracts: `machine-runtime`
- `remove this machine`
  - sanctioned adapter: `tools/vaws.py machine remove`
  - internal contracts: `machine-runtime`

## Internal State Touched

- `.workspace.local/servers.yaml`
- `.workspace.local/auth.yaml`
- `.workspace.local/state.json`

## Related Tests

- `tests/test_vaws_machine.py`
- `tests/test_workspace_skill_contracts.py`
- `tests/test_workspace_skill_routing_contracts.py`
