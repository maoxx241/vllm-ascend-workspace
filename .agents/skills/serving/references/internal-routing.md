# Internal Routing: Serving

Public contract: `../SKILL.md`

This file is a maintainer backstop. The public `SKILL.md` is the normal execution surface, and agents should not need this file to understand the default public recipe.
Use this file only for ambiguous routing, routing-maintenance work, or contract audits. Normal execution should stop at the public `SKILL.md` unless the next tool is genuinely unclear.

## Sanctioned Adapter Surface

- Ordinary execution surface: `tools/vaws.py serving start`, `tools/vaws.py serving status`, `tools/vaws.py serving list`, and `tools/vaws.py serving stop`
- Diagnostic companion surface: `tools/vaws.py doctor`
- Do not route a normal agent directly to `tools/lib/*.py`; library modules remain implementation owners behind the sanctioned adapter surface.

## Internal Delegation

### Public Action -> Internal Contract

- `serving` -> `branch-context`, `code-parity`, `runtime-environment`, `serving-lifecycle`

### Internal Contract -> Implementation Owner

- `branch-context` -> implemented by `tools/lib/branch_context.py`
- `code-parity` -> implemented by `tools/lib/code_parity.py`
- `runtime-environment` -> implemented by `tools/lib/runtime_env.py`
- `serving-lifecycle` -> discovery family `.agents/discovery/families/serving-lifecycle.yaml`
- `serving-session-cache` -> implemented by `tools/lib/serving_session.py`
- `serving-lifecycle-helper` -> implemented by `tools/lib/serving_lifecycle.py`

## Action Routing

- `start service for this model on the ready machine`
  - sanctioned adapter: `tools/vaws.py serving start`
  - family manifest: `.agents/discovery/families/serving-lifecycle.yaml`
  - lifecycle ladder: `serving.launch_service` -> `serving.probe_readiness` -> `serving.describe_session`
  - internal contracts: `branch-context`, `code-parity`, `runtime-environment`, `serving-lifecycle`
- `status service by service id`
  - sanctioned adapter: `tools/vaws.py serving status`
  - family manifest: `.agents/discovery/families/serving-lifecycle.yaml`
  - atomic tools: `serving.describe_session`
  - internal contracts: `serving-lifecycle`
- `list services for this workspace`
  - sanctioned adapter: `tools/vaws.py serving list`
  - family manifest: `.agents/discovery/families/serving-lifecycle.yaml`
  - atomic tools: `serving.list_sessions`
  - internal contracts: `serving-lifecycle`
- `stop service after validation or cleanup`
  - sanctioned adapter: `tools/vaws.py serving stop`
  - family manifest: `.agents/discovery/families/serving-lifecycle.yaml`
  - atomic tools: `serving.stop_service`
  - internal contracts: `serving-lifecycle`

## Internal State Touched

- remote service manifests under `/vllm-workspace/artifacts/services/`

## Related Tests

- `tests/test_vaws_serving.py`
- `tests/test_workspace_skill_contracts.py`
- `tests/test_workspace_skill_routing_contracts.py`
