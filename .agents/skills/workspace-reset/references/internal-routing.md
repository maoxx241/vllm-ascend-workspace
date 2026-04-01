# Internal Routing: Workspace Reset

Public contract: `../SKILL.md`

Use this file only after the reset contract has been selected.

## Command Mapping

- prepare: `tools/vaws.py reset --prepare`
- execute: `tools/vaws.py reset --execute --confirmation-id <id> --confirm <phrase>`

## Internal Behavior Notes

- preserve the guarded two-phase reset flow
- require `--confirmation-id <id>` and `--confirm <phrase>` for execute
- clear all managed servers on a best-effort basis
- downgrade the user-visible result to `partial` whenever any per-server cleanup result is `unreachable` or `cleanup_failed`, even if the command itself completes
- restore repo remotes to community defaults
- treat stale authorization state as invalid
- derive cleanup ownership from lifecycle state and current target handoff kind rather than bootstrap.completed

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
