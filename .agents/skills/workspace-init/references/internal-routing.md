# Internal Routing: Workspace Init

Public contract: `../SKILL.md`

Use this file only after the public `workspace-init` contract has been selected.

## Sanctioned Adapter Surface

- Ordinary execution surface: `tools/vaws.py init`
- Diagnostic companion surface: `tools/vaws.py doctor`
- Do not route a normal agent directly to `tools/lib/*.py`; library modules remain implementation owners behind the sanctioned adapter surface.

## Internal Delegation

### Public Action -> Internal Contract

- `workspace-init` -> `git-auth`, `repo-topology`, `machine-runtime`, `code-parity`, `runtime-environment`

### Internal Contract -> Implementation Owner

- `git-auth` -> implemented by `tools/lib/git_auth.py`
- `repo-topology` -> implemented by `tools/lib/repo_topology.py`
- `machine-runtime` -> implemented by `tools/lib/init_flow.py`, `tools/lib/machine.py`, and `tools/lib/remote.py`
- `code-parity` -> implemented by `tools/lib/code_parity.py`
- `runtime-environment` -> implemented by `tools/lib/runtime_env.py`

## Action Routing

- `prepare this repo for development`
  - sanctioned adapter: `tools/vaws.py init`
  - internal contracts: `git-auth`, `repo-topology`
- `first-time Git setup and first-machine setup`
  - sanctioned adapter: `tools/vaws.py init`
  - internal contracts: `git-auth`, `repo-topology`, `machine-runtime`, `code-parity`, `runtime-environment`

## Internal State Touched

- `.workspace.local/state.json`
- `.workspace.local/repos.yaml`
- `.workspace.local/auth.yaml`
- `.workspace.local/servers.yaml`

## Related Tests

- `tests/test_vaws_init.py`
- `tests/test_repo_layout.py`
- `tests/test_workspace_skill_contracts.py`
- `tests/test_workspace_skill_routing_contracts.py`
