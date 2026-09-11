# Consuming remote-dev

Status: current

Generic remote operations live in
[`vaws-remote-dev`](https://github.com/vllm-ascend-workspace/remote-dev).
This workspace installs the package and wires its MCP server with ordinary
configuration. remote-dev stays independently usable with explicit
`host` + `port`. See [target-state.md](target-state.md).

## 1. Installing

```bash
python .agents/scripts/vaws_deps.py sync
.vaws-local/venvs/linux/bin/python -c "import remote_dev.result, remote_dev.core.endpoint; print('ok')"
```

MCP: `.vaws-local/venvs/linux/bin/python -m remote_dev.mcp.server`.

## 2. What this workspace may inject

| Variable | Value | Why |
|---|---|---|
| `REMOTE_DEV_STATE_DIR` | `<repo>/.vaws-local/remote-dev-state` | Package-owned job records and logs |
| `REMOTE_DEV_SSH_MUX_DIR` | `~/.ssh/vaws-mux` | Shared multiplexed SSH directory |
| `REMOTE_DEV_DEFAULT_USER` | `root` | Ordinary default user |

Do **not** inject `REMOTE_DEV_RESOLVERS` or a global
`REMOTE_DEV_RUNTIME_ENV_FILE`. Coordinator supplies a complete launch
environment on managed executions. Ad-hoc remote-dev calls use the explicit
endpoint the caller already has.

## 3. Skill transport

Skills do not construct SSH options. For ordinary remote I/O they call
`.agents/lib/vaws_remote_dev.py` helpers that wrap `remote_dev.core.ssh_transport`
with an explicit endpoint mapping returned by coordinator or typed by the
user. They do not resolve `session_id` / `machine` through a consumer plugin.

## 4. Client wiring

`python3 .agents/scripts/vaws_client_setup.py` writes the `remote-dev` MCP
entry as `python -m remote_dev.mcp.server` with the env above. It must not
point the server at a workspace resolver.
