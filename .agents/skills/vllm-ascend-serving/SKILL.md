---
name: vllm-ascend-serving
description: Start, check, or stop a single-node vLLM Ascend service through coordinator TaskClient. Use for 拉服务 / 看服务状态 / 停掉服务. Do not use for machine bootstrap or generic remote I/O.
---

# vLLM Ascend Serving

Start, inspect, and stop one colocated `vllm serve` process as a **managed coordinator execution**. This skill owns model/preset/TP/PP/env args and health/first-token checks. It does not allocate NPUs, pick Python, or recover admission.

## Use this skill when

- the user asks to start, relaunch, check, or stop a vLLM-Ascend HTTP service

## Do not use when

- attaching a machine (coordinator owns `vaws-<user>` containers)
- generic remote file/shell work (remote-dev explicit host/port)
- benchmarks (use `vllm-ascend-benchmark`, which reuses this service's reference)

## Critical rules

- Task identity is `--context-file` / `VAWS_CONTEXT_FILE`. Never guess from cwd.
- One `TaskClient.run(command=..., resources=..., timeout_seconds=None, service=..., restart=...)` submits the service. Coordinator injects `VAWS_PYTHON`, `VAWS_SERVICE_PORT`, and `ASCEND_RT_VISIBLE_DEVICES`.
- `resources` holds `npu_count` or `devices`, plus `service_port` (`0` = first free declared port). Do not pass those as top-level run kwargs.
- `--relaunch` submits `restart=True` so the package stops the old named service and launches the new spec, including when only code changed.
- The launch command uses `"$VAWS_PYTHON"` and `"$VAWS_SERVICE_PORT"` and fails if they are unset. Do not pass a skill-selected interpreter. Generic container hostname `/etc/hosts` repair belongs to coordinator environment preparation, not this command.
- Queued / preparing / waiting / starting / uncertain are reported as `queued`, not as a failed launch and not as running. Health probes run only when the package returns a live endpoint and port.
- A bounded readiness timeout returns `incomplete` without asking the skill to discard ownership.
- Status/stop query coordinator facts by exact `--service` or `--execution-id`. They do not fall back to some other live execution on the task. Local JSON is a business config report for `--relaunch`, not a recovery ledger.
- Do not set reserved env `VAWS_PYTHON`, `VAWS_SERVICE_PORT`, or `ASCEND_RT_VISIBLE_DEVICES` via `--extra-env`.

## Entry points

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --model <remote-weight-path> \
  [--preset <name>] [--tp N] [--dp N] [--devices 0,1] \
  [--extra-env KEY=VALUE] [--port N] [--health-timeout S] \
  [--service vllm] [-- -- extra vllm args]
python3 .agents/skills/vllm-ascend-serving/scripts/serve_status.py [--service vllm] [--execution-id ID]
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py [--service vllm] [--execution-id ID] [--force]
```

`--relaunch` merges the last business config (model/tp/args). A new start after a live service is coordinator-owned association, not a workspace request-id retry.
