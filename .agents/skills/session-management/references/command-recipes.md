# Command Recipes

Inspect the native task and bind actual business worktrees through coordinator
`vaws session`. Do not call deleted `session_create.py` / `session_remove.py`
/ `npu_coordination.py`.

```bash
python3 .agents/scripts/vaws.py session
python3 .agents/skills/session-management/scripts/session_diff.py --context-file "$VAWS_CONTEXT_FILE"
python3 .agents/skills/session-management/scripts/session_gc.py --context-file "$VAWS_CONTEXT_FILE"
python3 .agents/skills/session-management/scripts/session_group.py create \
  --group-id pd \
  --member prefill=prefill \
  --member decode=decode \
  --context-file "$VAWS_CONTEXT_FILE"
python3 .agents/skills/session-management/scripts/session_group.py list
python3 .agents/skills/session-management/scripts/session_group.py teardown --group-id pd --context-file "$VAWS_CONTEXT_FILE"
```

Group teardown stops coordinator executions named by the group id and member
services. It does not delete `vaws-<user>`.
