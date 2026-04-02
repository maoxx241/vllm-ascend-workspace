# Internal Routing: Workspace Reset

Public contract: `../SKILL.md`

Use this file only after the public `workspace-reset` contract has been selected.

## Sanctioned Adapter Surface

- Ordinary execution surface: `tools/vaws.py reset prepare` followed by `tools/vaws.py reset execute`
- Diagnostic companion surface: `tools/vaws.py doctor`
- Do not route a normal agent directly to `tools/lib/*.py`; library modules remain implementation owners behind the sanctioned adapter surface.

## Internal Delegation

### Public Action -> Internal Contract

- `workspace-reset` -> `workspace-reset`, `machine-runtime`

### Internal Contract -> Implementation Owner

- `workspace-reset` -> implemented by `tools/lib/reset.py`
- `machine-runtime` -> implemented by `tools/lib/runtime.py` and `tools/lib/reset.py`

## Action Routing

- `reset this workspace`
  - sanctioned adapter: `tools/vaws.py reset prepare` then `tools/vaws.py reset execute`
  - internal contracts: `workspace-reset`, `machine-runtime`

## Internal State Touched

- `.workspace.local/servers.yaml`
- `.workspace.local/auth.yaml`
- `.workspace.local/repos.yaml`
- `.workspace.local/targets.yaml`
- `.workspace.local/state.json`
- `.workspace.local/sessions/`
- `.workspace.local/reset-request.json`
- git remotes under `vllm/` and `vllm-ascend/`

## Related Tests

- `tests/test_vaws_reset.py`
- `tests/test_workspace_skill_contracts.py`
- `tests/test_workspace_skill_routing_contracts.py`
