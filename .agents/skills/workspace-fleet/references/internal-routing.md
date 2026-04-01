# Internal Routing: Workspace Fleet

Public contract: `../SKILL.md`

Use this file only after the fleet contract has been selected.

## Command Mapping

- list: `tools/vaws.py fleet list`
- add: `tools/vaws.py fleet add`
- verify: `tools/vaws.py fleet verify`
- repair: no sanctioned CLI mapping yet; do not improvise one
- remove: no sanctioned CLI mapping yet; do not improvise one

## Internal Behavior Notes

- first-server runtime handoff and later server attachment use the same fleet path
- lifecycle readiness for fleet attachment and maintenance requires both `lifecycle.foundation.status == ready` and `lifecycle.git_profile.status == ready`
- when `--ssh-auth-ref` is omitted and exactly one safe profile exists, infer it
- when multiple safe profiles exist, stop and ask for clarification
- inventory state remains local-only
- unsupported repair or remove requests must be surfaced as blocked/unsupported

## Internal State Touched

- `.workspace.local/servers.yaml`
- `.workspace.local/state.json`

## Related Tests

- `tests/test_vaws_fleet.py`
