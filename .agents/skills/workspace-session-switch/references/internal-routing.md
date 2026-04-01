# Internal Routing: Workspace Session Switch

Public contract: `../SKILL.md`

Use this file only after the session-switch contract has been selected.

## Command Mapping

- create: `tools/vaws.py session create`
- switch: `tools/vaws.py session switch`
- status: `tools/vaws.py session status`

## Internal State Touched

- `.workspace.local/state.json`
- `.workspace.local/sessions/<session>/manifest.yaml`
- `/vllm-workspace/.vaws/sessions/<session>/manifest.yaml`

## Related Tests

- `tests/test_vaws_session.py`
