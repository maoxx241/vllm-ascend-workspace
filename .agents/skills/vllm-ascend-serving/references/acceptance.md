# Acceptance Criteria

- Task identity is `--context-file` / `VAWS_CONTEXT_FILE`.
- Start submits one `TaskClient.run` with `resources` (`npu_count` or
  `devices`, plus `service_port`) and `restart` for `--relaunch`.
- Queued / preparing / waiting is `status=queued` with the same `execution_id`. Health
  probes do not run until the execution is live with a port.
- Status and stop use exact `--service` or `--execution-id`. They do not
  guess another live execution.
- Reserved env `VAWS_PYTHON`, `VAWS_SERVICE_PORT`, and
  `ASCEND_RT_VISIBLE_DEVICES` cannot be set via `--extra-env`.
- Stop preserves the user container.
- Environment constraints (`recipe`, `python_abi`, `cann`, `soc`,
  `machine_type`) are forwarded even when recipe is omitted.
