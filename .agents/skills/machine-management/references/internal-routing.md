# Internal Routing: Machine Management

Public contract: `../SKILL.md`

This file is a maintainer backstop. The public `SKILL.md` is the normal execution surface, and agents should not need this file to understand the default public recipe.
Use this file only for ambiguous routing, routing-maintenance work, or contract audits. Normal execution should stop at the public `SKILL.md` unless the next tool is genuinely unclear.

## Sanctioned Adapter Surface

- Ordinary execution surface: `tools/vaws.py machine add`, `tools/vaws.py machine verify`, `tools/vaws.py machine list`, and `tools/vaws.py machine remove`
- Diagnostic companion surface: `tools/vaws.py doctor`
- Do not route a normal agent directly to `tools/lib/*.py`; library modules remain implementation owners behind the sanctioned adapter surface.

## Internal Delegation

### Public Action -> Internal Contract

- `machine-management` -> `.agents/discovery/families/machine-inventory.yaml`, `.agents/discovery/families/machine-runtime.yaml`, `code-parity`, `runtime-environment`

### Internal Contract -> Implementation Owner

- `machine-runtime` -> `tools/atomic/machine_probe_host_ssh.py`, `tools/atomic/machine_bootstrap_host_ssh.py`, `tools/atomic/machine_sync_workspace_mirror.py`, `tools/atomic/runtime_probe_container_transport.py`, `tools/atomic/runtime_reconcile_container.py`, `tools/atomic/runtime_bootstrap_container_transport.py`, `tools/atomic/runtime_cleanup_server.py`
- `code-parity` -> implemented by `tools/lib/code_parity.py`
- `runtime-environment` -> implemented by `tools/lib/runtime_env.py`

## Action Routing

- `attach this machine`
  - sanctioned adapter: `tools/vaws.py machine add`
  - discovery family: `.agents/discovery/families/machine-inventory.yaml`
  - inventory step: `machine.register_server`
  - discovery family: `.agents/discovery/families/machine-runtime.yaml`
  - repair ladder: `machine.bootstrap_host_ssh` -> `machine.sync_workspace_mirror` -> `runtime.reconcile_container` -> `runtime.bootstrap_container_transport`
- `check whether this machine is ready`
  - sanctioned adapter: `tools/vaws.py machine verify`
  - discovery family: `.agents/discovery/families/machine-inventory.yaml`
  - inventory probe: `machine.describe_server`
  - discovery family: `.agents/discovery/families/machine-runtime.yaml`
  - verify-first ladder: `machine.probe_host_ssh` -> `runtime.probe_container_transport`
- `repair machine readiness without touching service lifecycle`
  - sanctioned adapter: `tools/vaws.py machine verify`
  - discovery family: `.agents/discovery/families/machine-inventory.yaml`
  - inventory probe: `machine.describe_server`
  - discovery family: `.agents/discovery/families/machine-runtime.yaml`
  - repair ladder: `machine.bootstrap_host_ssh` -> `machine.sync_workspace_mirror` -> `runtime.reconcile_container` -> `runtime.bootstrap_container_transport`
- `list attached machines`
  - sanctioned adapter: `tools/vaws.py machine list`
  - discovery family: `.agents/discovery/families/machine-inventory.yaml`
  - inventory probe: `machine.list_servers`
- `remove this machine`
  - sanctioned adapter: `tools/vaws.py machine remove`
  - discovery family: `.agents/discovery/families/machine-inventory.yaml`
  - inventory mutation: `machine.remove_server`
  - discovery family: `.agents/discovery/families/machine-runtime.yaml`
  - cleanup path: `runtime.cleanup_server`

## Internal State Touched

- `.workspace.local/servers.yaml`
- `.workspace.local/auth.yaml`

## Related Tests

- `tests/test_vaws_machine.py`
- `tests/test_workspace_skill_contracts.py`
- `tests/test_workspace_skill_routing_contracts.py`
