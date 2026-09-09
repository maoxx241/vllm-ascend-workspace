# Behavior Reference

Use remote-dev companion tools for ad hoc remote read/edit/bash. This skill
owns one colocated `vllm serve` as a coordinator execution.

## Launch

1. Native task context (`--context-file` / `VAWS_CONTEXT_FILE`).
2. One `TaskClient.run(command, resources=..., environment=..., timeout_seconds=None, service=..., restart=...)`.
3. Coordinator injects `VAWS_PYTHON`, `VAWS_SERVICE_PORT`, and
   `ASCEND_RT_VISIBLE_DEVICES`. Hostname `/etc/hosts` repair is coordinator
   environment setup, not this command.
4. Queued / preparing / waiting / starting / uncertain is reported as `queued`. Health,
   models, and first-token run only when the package returns a live endpoint
   and port.
5. `--relaunch` is `restart=True`. Local serving JSON is a business config
   report, not a recovery ledger.

## Status and stop

Exact `--service` or `--execution-id` only. No fallback to some other live
execution on the task. Stop leaves the user container in place.

## Probe

`serve_probe_npus.py` is a host occupancy diagnostic. Pass `--host` or
`--execution-id`. It is not allocation authority.
