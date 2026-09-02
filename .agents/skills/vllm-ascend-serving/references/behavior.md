# Behavior Reference

## Optional prepared-runtime path

See [the shared coordinator](../../../coordinator/README.md) for pool bindings.
Legacy session wrappers remain separate. A pool launch uses an exact snapshot,
the host coordinator's active fence and declared free TCP ports, then records
the newly launched PID and heartbeats without waiting for model loading to
finish. Environment-ready, PID-created, and model-ready are distinct states.
Restart the service after development changes; do not reuse an old model
process to claim that new source has been tested.

## Relationship to remote-dev

Use `.remote-dev` tools for ad hoc remote read/edit/bash/search/patch around a
service. This skill owns service lifecycle semantics and keeps the existing
scripts as the managed VAWS compatibility backend.

## Escaping safety

The core value of this skill is that all SSH escaping is handled inside `serve_start.py`. The agent passes structured arguments via CLI flags; the script internally builds a bash script with proper `shlex.quote` on every dynamic value, then wraps the entire script for SSH transport. The agent should never construct raw `ssh ... "export ... && vllm serve ..."` commands for serving.

## Launch lifecycle

1. **resolve-target** — resolve the session from `--session-id` / `--session-file`, or auto-resolve from the nearest `.vaws-local/current-session.json` worktree binding (cwd upward); fail fast if none is found
2. **lock** — acquire the session serving lock so `start` and `stop` for the same session cannot race
3. **preflight** — with `--preset`, verify the recipe against the container (vllm version pin, absolute `PYTHONPATH` entries, JSON-valued serve args) before any running service is touched
4. **lease-gate** — require a nonempty live session NPU lease (`require_session_npu_lease`); an explicit `--devices` must be a lease subset, otherwise the first TP×DP devices of the sorted lease are used
5. **validate** — check model path exists remotely via `test -d` / `test -f` before touching any running service
6. **stop-existing** — if a previous service is recorded for that target, send SIGINT+SIGTERM (then SIGKILL if it survives) and wait for its devices to free
7. **parity-sync** — call `parity_sync.py` (unless `--skip-parity`)
8. **probe-npus** — check NPU device availability via `npu-smi info` on the host; validate the leased/requested devices against actual occupancy
9. **allocate-port** — allocate the service port through the session port lease (snapshot listening ports once, lease locally, then recheck the selected port before launch); no ad-hoc free-port scanning
10. **launch** — build and execute the launch script via SSH; on a failed launch or unparseable PID, a best-effort cleanup (PID file / stdout PID, kill + confirm) runs first and the port lease is released only when no leftover process is confirmed — otherwise the lease is kept and the result is `needs_repair`
11. **persist-starting-state** — after PID capture, write serving state with `status=starting`
12. **probe-health** — poll `GET /health` (HTTP 200)
13. **probe-models** — poll `GET /v1/models` (non-empty `data` array)
14. **probe-first-token** — one deterministic real request (`temperature 0`) before reporting ready
15. **persist-final-state** — update serving state to `ready` or `started`
16. **output** — print JSON to stdout

## Session targeting

`serve_start.py`, `serve_status.py`, and `serve_stop.py` are session-only. They accept optional `--session-id` or `--session-file`; when both are omitted, the session is auto-resolved by walking up from the current working directory to the nearest `.vaws-local/current-session.json` worktree binding, so running inside a session worktree needs zero target arguments. If no binding is found and no id/file is given, the command fails fast, telling the user to pass `--session-id` or create a session with `session-management`'s `session_create.py`. In all cases:

- the SSH endpoint comes from the session container
- parity is called with `parity_sync.py --session-id <id>`
- the service port is allocated and released through the session lease (`.vaws-local/sessions/leases.json`)
- leased NPU devices from the session are used as the default `ASCEND_RT_VISIBLE_DEVICES`
- relaunch and stop read only `.vaws-local/sessions/<id>/serving.json`
- stopping one session never reads or mutates another session's serving state
- `serve_start.py` and `serve_stop.py` use `.vaws-local/sessions/locks/<id>.serving.lock` to serialize lifecycle changes for the same session

`serve_probe_npus.py` is the exception with two mutually exclusive surfaces: `--machine <alias>` probes a registered machine host as a resource pool (machine-management scope), while `--session-id` / `--session-file` (or the cwd auto-bind) probes the session's base host.

## NPU device probing

Before launching, the script SSHes to the **bare-metal host** (port 22, via `host_endpoint()`) and runs `npu-smi info`. Host-level probing can see processes from **all** containers, bypassing PID namespace isolation. It determines:

- Total available NPU devices
- Which devices have running processes (PID-visible from the host)
- Which devices have high HBM usage (above 4096 MB), indicating occupancy even when PIDs are not visible from another container
- Which devices are free (no PID and HBM below threshold)

Device selection logic:

- Managed serving requires a nonempty live NPU lease for the session (`require_session_npu_lease`); an empty or stale lease fails closed with `needs_repair`.
- If `--devices` is explicitly given, it must be a subset of the session lease; those devices are validated against host occupancy and start returns `needs_input` with conflict details if any are busy.
- If `--devices` is not given, the launch uses the session's leased devices: the first `tp × dp` entries of the sorted lease (`tp` alone when `dp` is unset, the whole lease when neither is given). If the lease holds fewer devices than `tp × dp`, start returns `needs_input`. Free cards outside the lease are never auto-selected.
- If the host probe fails or returns malformed data, start fails closed with `status=blocked` and `phase=probe-npus`. Cooperative leases cannot exclude unmanaged host workloads, so the wrapper does not allocate a port or launch a process while occupancy is unknown.
- On relaunch, inherited `--devices` are re-validated against current availability.

## Relaunch merge rules

When `--relaunch` is used:

- Previous launch parameters are loaded from `.vaws-local/sessions/<session-id>/serving.json`.
- Any CLI argument provided this time **overrides** the previous value
- `--extra-env KEY=VALUE` is **merged** into the previous env map (new keys added, existing keys overwritten)
- `--unset-env KEY` **removes** a key from the inherited env map
- `--unset-args PREFIX` removes args starting with that prefix from the inherited extra args. Use `=` syntax (`--unset-args=--enforce-eager`) to avoid argparse treating the prefix as a separate flag. Boolean flags (where the next token starts with `-` or is absent) are removed alone; value-bearing flags (where the next token does not start with `-`) remove both the flag and its value.
- Extra vllm args after `--` are **appended** to inherited extra args
- Runtime-only fields (port, pid, log paths) are always recalculated

## Ascend environment

The launch script sources `/etc/profile.d/vaws-ascend-env.sh` if it exists and prepends the Ascend driver library paths to `LD_LIBRARY_PATH`. Device visibility is controlled via `ASCEND_RT_VISIBLE_DEVICES`.

## Custom CANN operators

`vllm-ascend` compiles custom CANN operators (e.g. `aclnnAddRmsNormBias`) into `vllm_ascend/_cann_ops_custom/`. The launch script dynamically discovers `set_env.bash` by:

1. Resolving the `vllm_ascend` package location via `import vllm_ascend`
2. Searching `_cann_ops_custom/` for `*/bin/set_env.bash` (vendor name is not hardcoded)
3. Sourcing the found script, which sets `ASCEND_CUSTOM_OPP_PATH` and adds `libcust_opapi.so` to `LD_LIBRARY_PATH`

After `remote-code-parity` sync, these build artifacts may be missing because they are untracked. Rebuild with:

```bash
cd /vllm-workspace/vllm-ascend && bash csrc/build_aclnn.sh /vllm-workspace/vllm-ascend ascend910b
```

Installation note: parity installs with the HuaweiCloud pip index and handles `numpy<2.0.0` (CANN hard dependency) automatically. Do not skip it or manually override numpy to >=2.0.

## Extra args escaping

Extra vllm args after `--` are passed to `vllm serve` as individual tokens. Each token is independently `shlex.quote`-wrapped for bash safety. This means JSON values like `--additional-config '{"key":"value"}'` are correctly preserved through the SSH + bash layers — double quotes inside JSON are not consumed.

Tokens written into the `_serve.sh` heredoc (model path, served model name, extra args) must not contain newlines; such values are rejected with `needs_input` before launch, because a literal newline could split the heredoc body or terminate it early.

The args are stored in local state as a flat list of strings. On relaunch, the inherited list is used as-is without re-splitting.

## Process detachment

Services are launched with `nohup ... </dev/null &` followed by `disown` to fully detach from the SSH session. The PID is captured and written to `<runtime_dir>/pid`.

## Remote runtime directory

Each launch instance gets its own directory under:

```
<workdir>/.vaws-runtime/serving/<timestamp>/
```

With a configured unified alias, the layout is:

```
<workdir>/.vaws-runtime/serving/<alias>/<timestamp>/
```

The service receives `VAWS_AGENT_ID`; configured aliases are also exported as
`VAWS_AGENT_ALIAS` and `VAWS_PROJECT_ALIAS`. Serving state snapshots those
values. A missing or declined alias preserves the legacy directory layout.

This directory contains:
- `stdout.log` — vllm server stdout
- `stderr.log` — vllm server stderr
- `pid` — process ID file

The `<workdir>` comes from the session record (typically `/vllm-workspace`).

The vLLM process is launched **from** this runtime directory, not from `/vllm-workspace`, to prevent Python from resolving the `vllm` package to the source tree instead of the installed package.

## Stop sequence

1. `SIGINT` (graceful shutdown)
2. Wait 5 seconds
3. `SIGTERM` if still alive
4. Wait 5 seconds
5. `SIGKILL` only if `--force` is given

## Status probes

- **alive**: `kill -0 <pid>` succeeds
- **health**: `GET /health` returns HTTP 200
- **models_ok**: `GET /v1/models` returns a non-empty `data` array

Combined status:
- `ready` = alive + health + models_ok
- `alive_healthy` = alive + health but models not confirmed
- `alive` = process exists but health endpoint not responding
- `stopped` = process does not exist
