# Internal Routing: Workspace Init

Public contract: `../SKILL.md`

This file is a maintainer backstop. The public `SKILL.md` is the normal execution surface, and agents should not need this file to understand the default public recipe.

## Sanctioned Adapter Surface

- Ordinary execution surface: `.agents/discovery/families/workspace-foundation.yaml`, `.agents/discovery/families/workspace-diagnostics.yaml`, and when a first machine is requested `.agents/discovery/families/machine-inventory.yaml` plus `.agents/discovery/families/machine-runtime.yaml`
- Diagnostic companion surface: `tools/vaws.py doctor`
- Do not route a normal agent directly to `tools/lib/*.py`; library modules remain implementation owners behind the sanctioned adapter surface.

## Internal Delegation

### Public Action -> Internal Contract

- `workspace-init` -> `workspace-foundation`, `workspace-diagnostics`, and optional `machine-inventory` plus `machine-runtime`

### Internal Contract -> Implementation Owner

- `workspace-foundation` -> `tools/atomic/workspace_probe_config_validity.py`, `tools/atomic/workspace_probe_git_auth.py`, `tools/atomic/workspace_probe_repo_topology.py`, `tools/atomic/workspace_probe_submodules.py`, `tools/atomic/workspace_describe_repo_targets.py`
- `workspace-diagnostics` -> `tools/atomic/workspace_diagnose_overlay.py`, `tools/atomic/workspace_diagnose_workspace.py`
- `machine-inventory` -> `tools/atomic/machine_register_server.py`, `tools/atomic/machine_describe_server.py`, `tools/atomic/machine_list_servers.py`
- `machine-runtime` -> `tools/atomic/machine_probe_host_ssh.py`, `tools/atomic/machine_bootstrap_host_ssh.py`, `tools/atomic/machine_sync_workspace_mirror.py`, `tools/atomic/runtime_probe_container_transport.py`, `tools/atomic/runtime_reconcile_container.py`, `tools/atomic/runtime_bootstrap_container_transport.py`

## Action Routing

- `prepare this repo for development`
  - discovery families: `.agents/discovery/families/workspace-foundation.yaml`, `.agents/discovery/families/workspace-diagnostics.yaml`
  - local foundation ladder: `workspace.probe_config_validity` -> `workspace.probe_git_auth` -> `workspace.probe_repo_topology` -> `workspace.probe_submodules` -> `workspace.describe_repo_targets`
- `first-time Git setup and first-machine setup`
  - discovery families: `.agents/discovery/families/workspace-foundation.yaml`, `.agents/discovery/families/machine-inventory.yaml`, `.agents/discovery/families/machine-runtime.yaml`
  - first-machine ladder: `machine.register_server` -> `machine.probe_host_ssh` -> `runtime.probe_container_transport`
  - repair ladder: `machine.bootstrap_host_ssh` -> `machine.sync_workspace_mirror` -> `runtime.reconcile_container` -> `runtime.bootstrap_container_transport`
- `diagnose why init cannot finish`
  - sanctioned adapter: `tools/vaws.py doctor`
  - discovery family: `.agents/discovery/families/workspace-diagnostics.yaml`
  - diagnostic tool: `workspace.diagnose_workspace`

## Internal State Touched

- `.workspace.local/repos.yaml`
- `.workspace.local/auth.yaml`
- `.workspace.local/servers.yaml`

## Related Tests

- `tests/test_workspace_skill_recipe_contracts.py`
- `tests/test_repo_layout.py`
- `tests/test_workspace_skill_contracts.py`
- `tests/test_workspace_skill_routing_contracts.py`
