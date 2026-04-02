# Internal Routing: Serving

Public contract: `../SKILL.md`

Use this file only after the public `serving` contract has been selected.

## Sanctioned Adapter Surface

- Ordinary execution surface: `tools/vaws.py serving start`, `tools/vaws.py serving status`, `tools/vaws.py serving list`, and `tools/vaws.py serving stop`
- Diagnostic companion surface: `tools/vaws.py doctor`
- Do not route a normal agent directly to `tools/lib/*.py`; library modules remain implementation owners behind the sanctioned adapter surface.

## Internal Delegation

### Public Action -> Internal Contract

- `serving` -> `branch-context`, `code-parity`, `runtime-environment`, `serving-session`

### Internal Contract -> Implementation Owner

- `branch-context` -> implemented by `tools/lib/branch_context.py`
- `code-parity` -> implemented by `tools/lib/code_parity.py`
- `runtime-environment` -> implemented by `tools/lib/runtime_env.py`
- `serving-session` -> implemented by `tools/lib/serving.py`

## Action Routing

- `start service for this model on the ready machine`
  - sanctioned adapter: `tools/vaws.py serving start`
  - internal contracts: `branch-context`, `code-parity`, `runtime-environment`, `serving-session`
- `status service by service id`
  - sanctioned adapter: `tools/vaws.py serving status`
  - internal contracts: `serving-session`
- `list services for this workspace`
  - sanctioned adapter: `tools/vaws.py serving list`
  - internal contracts: `serving-session`
- `stop service after validation or cleanup`
  - sanctioned adapter: `tools/vaws.py serving stop`
  - internal contracts: `serving-session`

## Internal State Touched

- `.workspace.local/state.json`
- `serving/`

## Related Tests

- `tests/test_vaws_serving.py`
- `tests/test_workspace_skill_contracts.py`
- `tests/test_workspace_skill_routing_contracts.py`
