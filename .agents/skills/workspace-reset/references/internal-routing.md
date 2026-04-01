# Internal Routing: Workspace Reset

Public contract: `../SKILL.md`

Use this file only after the public `workspace-reset` contract has been selected.

## Internal Delegation

### Public Action -> Internal Contract

- `workspace-reset` -> `workspace-reset`, `machine-runtime`

### Internal Contract -> Backend

- `workspace-reset` -> `tools/lib/reset.py::prepare_reset()` and the tracked reset authorization flow under `tools/vaws.py`
- `machine-runtime` -> persisted machine state in `tools/lib/runtime.py` plus machine cleanup performed during `tools/lib/reset.py`

## Action Routing

- `reset this workspace`
  - internal contracts: `workspace-reset`, `machine-runtime`
  - backend entrypoints: `tools/lib/reset.py::prepare_reset()` followed by the reset execute flow under `tools/vaws.py`

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
