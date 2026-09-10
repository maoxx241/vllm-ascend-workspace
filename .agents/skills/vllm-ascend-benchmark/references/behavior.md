# Behavior

Custom commands are allowed through coordinator `vaws_run`. Scripts and presets remain the convenient path. Unparseable results stay original text with unknown status.

1. Resolve the native task from `--context-file` / `VAWS_CONTEXT_FILE`.
2. A live service is `--execution-id` or `--service`.
3. A new start uses serving `serve_start.py`. Queued / preparing is reported
   as `queued` with the same execution id; do not force-stop or resubmit.
4. Live `--execution-id` still requires health/models/first-token evidence.
5. Stop only a service this benchmark started.
6. Persist results under `.vaws-local/tasks/<task-id>/benchmark/runs/`.
