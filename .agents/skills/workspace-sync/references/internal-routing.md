# Internal Routing: Workspace Sync

Public contract: `../SKILL.md`

Use this file only after the sync contract has been selected.

## Command Mapping

- status: `tools/vaws.py sync status`
- start: `tools/vaws.py sync start <session_name>`
- done: `tools/vaws.py sync done`
- compatibility wrapper: `./sync`

## Internal Behavior Notes

- `sync status` delegates to session status reporting
- `sync start` creates and switches to the target session
- `sync done` is a reserved compatibility completion step
- compatibility behavior stays internal
- sync is session-oriented and does not own init or fleet responsibilities

## Related Tests

- `tests/test_vaws_sync.py`
