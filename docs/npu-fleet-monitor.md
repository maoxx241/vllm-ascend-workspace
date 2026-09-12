# Local NPU fleet monitor

Status: current

The workspace launches the independently released [vaws-top package](https://github.com/vllm-ascend-workspace/vaws-top) through uvx. Its package owns fleet queries, the UI and observations. Coordinator owns provisioning and allocation.

## Start and inspect

Run from the workspace on Windows, WSL, Linux or macOS:

```text
uv run --no-project python .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py start
uv run --no-project python .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py status
uv run --no-project python .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py stop
```

Start installs or reuses the release wheel, launches a hidden background process on Windows, and probes the loopback health endpoint. The JSON result contains the URL, log, CLI/MCP prefixes and the package skill URL. Read the package skill for fleet-query methods. An optional `deploy` command verifies the packaged frontend without starting a service.

The release tag in `manage_monitor.py` is the version source. A release wheel is required because it includes the built frontend. `--from` or `VAWS_TOP_FROM` can select a development artifact; source builds require the frontend build dependencies.

## Lifecycle and ownership

Untracked state lives under the shared workspace's `.vaws-local/npu-fleet-monitor/`: `serve.json` contains the PID, birth time, command, port and installation source; `serve.log` holds output; `data/` belongs to vaws-top. Restarts preserve these files.

Start and status accept a running instance only when its birth time and command match the saved identity. Stop validates the same identity before terminating the process tree, and restart proceeds only after stop succeeds. A legacy or mismatched live PID is reported as unverified and is left alone. A dead PID record can be removed. This prevents PID reuse from stopping an unrelated process.
An already listening port without a matching record is a conflict. If a POSIX
leader exits while its group remains, stop reports the remaining group and
preserves the record; it does not claim success or force-kill an unverified group.

## Configuration

The listener stays on `127.0.0.1`; `--port` changes its port. Inventory and host-pool inputs use explicit flags, then caller environment, then existing shared workspace defaults. Host-key bootstrap is explicitly configured with `--bootstrap-command` or `NFM_BOOTSTRAP_COMMAND`.

Remote container provisioning and its user identity belong to coordinator's provision interface. Monitor observations carry `allocation_authority: false`; a managed execution does not require a monitor query first.
