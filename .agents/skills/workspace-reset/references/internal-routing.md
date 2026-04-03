# Internal Routing: Workspace Reset

Public contract: `../SKILL.md`

This file is a maintainer backstop. The public `SKILL.md` is the normal execution surface, and agents should not need this file to understand the default public recipe.
Use this file only for ambiguous routing, routing-maintenance work, or contract audits. Normal execution should stop at the public `SKILL.md` unless the next tool is genuinely unclear.

## Sanctioned Adapter Surface

- Ordinary execution surface: `tools/vaws.py reset prepare` plus `.agents/discovery/families/reset-cleanup.yaml`
- Diagnostic companion surface: `tools/vaws.py doctor`
- Do not route a normal agent directly to `tools/lib/*.py`; library modules remain implementation owners behind the sanctioned adapter surface.

## Internal Delegation

### Public Action -> Internal Contract

- `workspace-reset` -> `reset-cleanup`

### Internal Contract -> Implementation Owner

- `reset-cleanup` -> `tools/atomic/reset_prepare_request.py`, `tools/atomic/reset_cleanup_remote_runtime.py`, `tools/atomic/reset_cleanup_overlay.py`, `tools/atomic/reset_cleanup_known_hosts.py`, `tools/atomic/reset_restore_public_remotes.py`

## Action Routing

- `reset this workspace`
  - sanctioned adapter: `tools/vaws.py reset prepare`
  - discovery family: `.agents/discovery/families/reset-cleanup.yaml`
  - destructive sequence: `reset.prepare_request` -> `reset.cleanup_remote_runtime` -> `reset.cleanup_overlay` -> `reset.cleanup_known_hosts` -> `reset.restore_public_remotes`
- `diagnose why reset cannot finish`
  - sanctioned adapter: `tools/vaws.py doctor`
  - discovery families: `.agents/discovery/families/workspace-diagnostics.yaml`, `.agents/discovery/families/reset-cleanup.yaml`

## Internal State Touched

- `.workspace.local/servers.yaml`
- `.workspace.local/auth.yaml`
- `.workspace.local/repos.yaml`
- `.workspace.local/reset-request.json`
- `.workspace.local/benchmark-runs/`
- git remotes under `vllm/` and `vllm-ascend/`

## Related Tests

- `tests/test_vaws_reset.py`
- `tests/test_workspace_skill_contracts.py`
- `tests/test_workspace_skill_routing_contracts.py`
