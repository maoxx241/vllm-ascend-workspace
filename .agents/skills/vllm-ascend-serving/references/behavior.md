# Behavior Reference

## Escaping safety

The core value of this skill is that all SSH escaping is handled inside `serve_start.py`. The agent passes structured arguments via CLI flags; the script internally builds a bash script with proper `shlex.quote` on every dynamic value, then wraps the entire script for SSH transport. The agent should never construct raw `ssh ... "export ... && vllm serve ..."` commands for serving.

## Launch lifecycle

1. **resolve-machine** — look up the machine alias in `.vaws-local/machine-inventory.json`
2. **stop-existing** — if a previous service is recorded, send SIGINT+SIGTERM
3. **parity-sync** — call `parity_sync.py` (unless `--skip-parity`)
4. **probe-npus** — check NPU device availability via `npu-smi info`; validate or auto-select devices
5. **validate** — check model path exists remotely via `test -d` / `test -f`
6. **allocate-port** — find a free port in 30000–60000 range by trying `socket.bind` remotely
7. **launch** — build and execute the launch script via SSH
8. **probe-health** — poll `GET /health` (HTTP 200)
9. **probe-models** — poll `GET /v1/models` (non-empty `data` array)
10. **persist-state** — write `.vaws-local/serving/<alias>.json`
11. **output** — print JSON to stdout

## NPU device probing

Before launching, the script SSHes to the **bare-metal host** (port 22, via `host_endpoint()`) and runs `npu-smi info`. Host-level probing can see processes from **all** containers, bypassing PID namespace isolation. It determines:

- Total available NPU devices
- Which devices have running processes (PID-visible from the host)
- Which devices have high HBM usage (above 4096 MB), indicating occupancy even when PIDs are not visible from another container
- Which devices are free (no PID and HBM below threshold)

Device selection logic:

- If `--devices` is explicitly given, those specific devices are validated. If any are occupied, start returns `needs_input` with conflict details.
- If `--devices` is not given but `--tp` is, the first `tp` free devices are auto-selected. If not enough free devices exist, returns `needs_input`.
- If neither is given, no device filtering is applied.
- If `npu-smi` itself fails (e.g. driver not found), the probe is treated as non-fatal and launch proceeds with whatever devices the user specified.
- On relaunch, inherited `--devices` are re-validated against current availability.

## Relaunch merge rules

When `--relaunch` is used:

- Previous launch parameters are loaded from `.vaws-local/serving/<alias>.json`
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

Installation note: `pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple` handles `numpy<2.0.0` (CANN hard dependency) automatically. Do not skip it or manually override numpy to >=2.0.

## Extra args escaping

Extra vllm args after `--` are passed to `vllm serve` as individual tokens. Each token is independently `shlex.quote`-wrapped for bash safety. This means JSON values like `--additional-config '{"key":"value"}'` are correctly preserved through the SSH + bash layers — double quotes inside JSON are not consumed.

The args are stored in local state as a flat list of strings. On relaunch, the inherited list is used as-is without re-splitting.

## Process detachment

Services are launched with `nohup ... </dev/null &` followed by `disown` to fully detach from the SSH session. The PID is captured and written to `<runtime_dir>/pid`.

## Remote runtime directory

Each launch instance gets its own directory under:

```
<workdir>/.vaws-runtime/serving/<timestamp>/
```

This directory contains:
- `stdout.log` — vllm server stdout
- `stderr.log` — vllm server stderr
- `pid` — process ID file

The `<workdir>` comes from the inventory record (typically `/vllm-workspace`).

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
