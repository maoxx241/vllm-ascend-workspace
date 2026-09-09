# Consuming remote-dev

Status: current

The remote development substrate used to live in this repository at
`.remote-dev/`. It is now the public package
[`vaws-remote-dev`](https://github.com/vllm-ascend-workspace/remote-dev)
(`v0.1.0`, locked by `uv.lock`). This document is the consumer-side
contract.

Phase 1 of the sequenced plan recorded in [target-state.md](target-state.md).

## 1. Why a package

| Option | Why not |
|---|---|
| **Git submodule** at `.remote-dev/` | Couples cloners to a second repository during `git submodule update`. |
| **Vendored copy** | Imports keep succeeding against stale code. |
| **External checkout** (previous) | Required a launcher, a checkout-root environment variable, and a hand-written SHA pin. The local launcher shadowed the package name. |
| **Installed dependency** (chosen) | `uv sync` installs `remote_dev` from git+https. MCP is `python -m remote_dev.mcp.server`. |

**Consequence:** cloning this scaffold without `uv sync` does not give you
the remote-dev tools. Local work, docs work and every Git task are
unaffected. Remote endpoint work requires the installed package; without it,
`vaws_deps.py doctor` reports `remote_endpoints` unavailable and the MCP
server cannot start.

## 2. Installing

```bash
uv sync
.venv/bin/python -c "import remote_dev.result, remote_dev.core.endpoint; print('ok')"
```

`uv.lock` records the git commit. Do not copy that SHA into workflows.

## 3. What the scaffold injects

The package is ignorant of this scaffold, so everything it needs is
configuration. Tracked MCP files and `vaws_client_setup.py` set:

| Variable | Value | Why |
|---|---|---|
| `REMOTE_DEV_RESOLVERS` | `.agents/lib/vaws_remote_dev_plugin.py:setup` | The scaffold's endpoint resolver. Without it the substrate knows only `host`+`port` and `alias`. |
| `REMOTE_DEV_RUNTIME_ENV_FILE` | `/etc/profile.d/vaws-ascend-env.sh` | The Ascend profile the workspace containers install. |
| `REMOTE_DEV_STATE_DIR` | `<repo>/.vaws-local/remote-dev-state` | Job records, read ledgers, logs. |
| `REMOTE_DEV_SSH_MUX_DIR` | `~/.ssh/vaws-mux` | Shared multiplexed SSH directory. |
| `REMOTE_DEV_DEFAULT_USER` / `_ROOT` / `_CWD` | `root`, `/vllm-workspace`, `/vllm-workspace` | MCP session defaults. |

The package does not read a former checkout-root environment variable.

## 4. The resolver contract

`.agents/lib/vaws_remote_dev_plugin.py` registers one resolver, `vaws`, with
`fields = ("machine", "session_id", "session_file")`. It is the scaffold-side
reimplementation of the substrate's deleted
`core/endpoint.py::_endpoint_from_managed`. Import `remote_dev.core.endpoint`
from the package; there is no `remote_dev.endpoint`.

## 5. Client wiring

| Former in-tree path | Current |
|---|---|
| `python3 .agents/scripts/remote_dev.py server` | `.venv/bin/python -m remote_dev.mcp.server` |
| `python3 .agents/scripts/remote_dev.py tool remote_bash ...` | `remote-dev bash ...` or `uv run remote-dev bash ...` |
| `python3 .agents/scripts/remote_dev.py hook claude` | `.venv/bin/python -m remote_dev.hooks.claude_remote_guard` |

The local launcher `.agents/scripts/remote_dev.py` is deleted so
`import remote_dev` resolves to the package.
