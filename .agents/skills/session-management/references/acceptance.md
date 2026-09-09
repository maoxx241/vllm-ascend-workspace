# Acceptance

- Task identity is `--context-file` / `VAWS_CONTEXT_FILE`. cwd and chat
  history are not task identity.
- `session_diff.py` summarizes bound worktrees from the native task context.
- `session_group.py create` writes `members[{name,service}]` and
  `context_file` on the group. That shape is accepted by PD `plan`.
- Member services are unique. Duplicate services fail closed.
- `session_group.py teardown` calls coordinator `observe(stop)` on the group
  id and member services. The user container is preserved.
- `session_gc.py` reports unresolved executions and never auto-deletes
  containers or releases devices by age.
- Deleted commands are not part of this skill: `session_create.py`,
  `session_list.py`, `session_status.py`, `session_remove.py`,
  `npu_coordination.py`.
