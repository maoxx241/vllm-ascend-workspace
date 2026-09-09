# Acceptance

- Task identity is `--context-file` / `VAWS_CONTEXT_FILE`.
- Queued or preparing serve is `status=queued` with the same execution id and
  is not force-stopped.
- A supplied `--execution-id` is not stopped on error.
- Live `--execution-id` requires business readiness, not `target.live` alone.
- Results land under `.vaws-local/tasks/<task-id>/benchmark/runs/`.
