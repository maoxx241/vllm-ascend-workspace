# Internal Routing: Workspace Session Switch

Public contract: `../SKILL.md`

Use this file only after the session-switch contract has been selected.

## Command Mapping

- create: `tools/vaws.py session create`
- switch: `tools/vaws.py session switch`
- status: `tools/vaws.py session status`

## Internal Behavior Notes

- session create and switch require a fleet-owned current target or a compatible legacy target handoff
- keep current_target and current_target_kind authoritative for session lifecycle resolution
- session switching does not own server attachment or init orchestration

## Internal State Touched

- `.workspace.local/state.json`
- `.workspace.local/sessions/<session>/manifest.yaml`
- `/vllm-workspace/.vaws/sessions/<session>/manifest.yaml`

## Related Tests

- `tests/test_vaws_session.py`
