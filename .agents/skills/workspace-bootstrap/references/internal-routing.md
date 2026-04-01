# Internal Routing: Workspace Bootstrap

Public contract: `../SKILL.md`

Use this file only after the compatibility alias has been selected.

## Command Mapping

- compatibility alias: `tools/vaws.py init --bootstrap`
- primary staged route: `tools/vaws.py init`

## Internal Behavior Notes

- keep bootstrap wording mapped to staged init semantics
- do not create a separate bootstrap runtime implementation
- defer local readiness, git topology, and server attachment to their first-class skills

## Internal State Touched

- `.workspace.local/state.json`
- `.workspace.local/repos.yaml`
- `.workspace.local/auth.yaml`
- `.workspace.local/servers.yaml`
- `.workspace.local/targets.yaml`

## Related Tests

- `tests/test_vaws_init_bootstrap.py`
