# Internal Routing: Workspace Init

Public contract: `../SKILL.md`

Use this file only after the staged init contract has been selected.

## Command Mapping

- primary: `tools/vaws.py init`
- compatibility bootstrap alias: `tools/vaws.py init --bootstrap`

## Internal Behavior Notes

- route local prerequisite work through foundation before git profile or fleet
- keep re-initialization idempotent when the existing staged state already matches the request
- treat first server attachment as fleet-owned, not init-owned

## Internal State Touched

- `.workspace.local/state.json`
- `.workspace.local/repos.yaml`
- `.workspace.local/auth.yaml`
- `.workspace.local/servers.yaml`
- `.workspace.local/targets.yaml`

## Related Tests

- `tests/test_vaws_init.py`
- `tests/test_vaws_init_bootstrap.py`
