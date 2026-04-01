# Internal Routing: Workspace Foundation

Public contract: `../SKILL.md`

Use this file only after the foundation contract has been selected.

## Command Mapping

- primary: `tools/vaws.py foundation`

## Internal Behavior Notes

- foundation reports local prerequisite readiness only
- degraded is acceptable when only recommended tooling is missing
- blocked means a required prerequisite must be fixed before init can proceed

## Internal State Touched

- `.workspace.local/state.json`

## Related Tests

- `tests/test_vaws_foundation.py`
