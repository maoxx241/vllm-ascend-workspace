---
name: session-management
description: Bind actual business worktrees and inspect local source diffs. Remote runtime, containers, and NPU leases belong to coordinator vaws_session/vaws_run/vaws_finish.
---

# Session / task identity

Local task identity is the native client context passed to the coordinator
package. This skill does not create Docker containers or reserve NPUs.

## Use this skill when

- binding actual vllm / vllm-ascend worktrees
- summarizing local git diffs of those bound sources
- grouping PD business roles (names only)

## Do not use when

- creating a per-task container
- allocating NPUs or service ports
- guessing a task from cwd or chat history

## Entry points

```bash
python3 .agents/scripts/vaws.py session
python3 .agents/scripts/vaws.py session --sources vllm=... --sources vllm-ascend=...
python3 .agents/skills/session-management/scripts/session_diff.py
python3 .agents/skills/session-management/scripts/session_gc.py
python3 .agents/skills/session-management/scripts/session_group.py create --group-id pd --member prefill=prefill --member decode=decode --context-file "$VAWS_CONTEXT_FILE"
python3 .agents/skills/session-management/scripts/session_group.py teardown --group-id pd

`session_group.py teardown` stops coordinator executions named by the group id
and member services. It never deletes the user container.
```

`session_gc.py` reports unresolved coordinator executions. It never auto-deletes containers or releases resources by age.
