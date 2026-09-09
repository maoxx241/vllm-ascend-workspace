# Behavior Reference

## Task identity

Local task identity is the native client context (`--context-file` /
`VAWS_CONTEXT_FILE`). This skill does not create Docker containers, reserve
NPUs, or guess a task from cwd or chat history.

Source bindings point at actual Git worktrees via coordinator
`TaskClient.sources`. Worktrees may change between executions after existing
runtime bindings are returned, without changing the VAWS task id.

Remote runtime, containers, NPU leases, and process lifecycle belong to
coordinator `vaws_session` / `vaws_run` / `vaws_finish`.

## Groups

`session_group.py create` records task-scoped business service names for
multi-role work such as PD. Members are `name=service`. The group file stores
`context_file` on the group. Teardown calls coordinator `observe(stop)` on
the group id and each member service. It never deletes the user container.

## GC

`session_gc.py` reports unresolved coordinator executions. Age, local PID
death, and missing local metadata are not release evidence.
