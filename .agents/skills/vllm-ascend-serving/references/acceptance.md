# Acceptance Criteria

## A1. Fresh start — structured input

**Given** a ready managed machine with a valid model path,
**When** `serve_start.py --machine <alias> --model <path> --tp 4` runs,
**Then**:
- parity is executed before launching
- a free port is auto-allocated
- the service starts and `/health` returns 200
- `/v1/models` returns the served model
- stdout is a JSON object with `status=ready`, `base_url`, `pid`, `log_stdout`, `log_stderr`
- `.vaws-local/serving/<alias>.json` is written

## A2. Fresh start — model path not found

**Given** a ready machine,
**When** `--model /nonexistent/path` is passed,
**Then** returns `status=needs_input` with a clear error before launching.

## A3. Relaunch — same config

**Given** a previous successful start recorded in `.vaws-local/serving/<alias>.json`,
**When** `serve_start.py --machine <alias> --relaunch` runs,
**Then**:
- previous model, tp, devices, env, extra_args are reused
- port is re-allocated (not inherited)
- parity runs again
- service starts successfully

## A4. Relaunch — add env delta

**Given** a previous successful start,
**When** `--relaunch --extra-env VLLM_LOGGING_LEVEL=DEBUG`,
**Then** the new env appears in the launched service alongside all previous env vars.

## A5. Relaunch — remove env

**Given** a previous start with `VLLM_LOGGING_LEVEL=DEBUG`,
**When** `--relaunch --unset-env VLLM_LOGGING_LEVEL`,
**Then** that env var is absent from the new launch.

## A6. Relaunch — override model

**Given** a previous start with model A,
**When** `--relaunch --model /path/to/modelB --served-model-name modelB`,
**Then** the service starts with model B.

## A7. Status — service running

**Given** a running service,
**When** `serve_status.py --machine <alias>` runs,
**Then** returns `status=ready`, `alive=true`, `health=true`, `models_ok=true`.

## A8. Status — service stopped

**Given** a stopped service,
**When** `serve_status.py` runs,
**Then** returns `status=stopped`, `alive=false`, and includes `stderr_tail`.

## A9. Status — no previous service

**Given** no prior start for this machine,
**When** `serve_status.py` runs,
**Then** returns `status=not_found`.

## A10. Stop — graceful

**Given** a running service,
**When** `serve_stop.py --machine <alias>` runs,
**Then**:
- sends SIGINT first
- waits, then SIGTERM if needed
- does NOT SIGKILL unless `--force`
- updates local state to `stopped`
- returns `status=stopped`

## A11. Stop — force

**Given** a service that ignores SIGINT/SIGTERM,
**When** `serve_stop.py --force`,
**Then** sends SIGKILL and returns `status=stopped`.

## A12. Parity gate

**Given** a ready machine where parity fails,
**When** `serve_start.py` runs (without `--skip-parity`),
**Then** returns `status=blocked` and does not launch.

## A13. Skip parity

**When** `serve_start.py --skip-parity` runs,
**Then** parity is not invoked and launch proceeds.

## A14. Previous service cleanup

**Given** a running service on the target machine,
**When** a new `serve_start.py` runs for the same machine,
**Then** the old service is stopped before the new one launches.

## A15. NPU probe — devices busy (cross-container via host)

**Given** a machine where NPU 0 and 1 have running processes (possibly from other containers),
**When** `serve_start.py --devices 0,1,2,3` is called,
**Then** returns `status=needs_input` with conflict details showing which devices are busy (detected via host-level `npu-smi` with PID and/or HBM threshold).

## A16. NPU probe — auto-select

**Given** a machine with 8 NPUs where 0,1 are busy and 2-7 are free,
**When** `serve_start.py --tp 4` (no `--devices`) is called,
**Then** auto-selects 4 free devices (e.g. `2,3,4,5`) and launches successfully.

## A17. NPU probe — not enough free

**Given** a machine with 2 free NPUs,
**When** `serve_start.py --tp 4` is called,
**Then** returns `status=needs_input` explaining only 2 NPUs are free.

## A18. NPU probe standalone

**When** `serve_probe_npus.py --machine <alias>` runs,
**Then** returns JSON with `devices`, `busy` (with PID details), `hbm` (per-device HBM usage), `free`, `free_count`, and `hbm_busy_threshold_mb`. Probing is done on the bare-metal host for cross-container visibility.

## A19. Escaping safety

**Given** model paths, env values, or args containing spaces, quotes, or shell metacharacters,
**When** any serve script runs,
**Then** the values are correctly escaped and the remote command executes without shell injection or breakage.

## A20. Custom CANN operators

**Given** `vllm-ascend` has been rebuilt with `csrc/build_aclnn.sh`,
**When** `serve_start.py` launches a model requiring custom ops (e.g. `aclnnAddRmsNormBias`),
**Then** the launch script automatically sources the custom ops `set_env.bash` and the model loads without `libopapi.so` errors.

## A21. Launch directory isolation

**When** `serve_start.py` launches vLLM,
**Then** the process working directory is the runtime dir (e.g. `/vllm-workspace/.vaws-runtime/serving/<ts>/`), NOT `/vllm-workspace`, preventing Python from resolving `import vllm` to the source tree.

## A22. Unset boolean flag

**Given** a previous launch with `--enforce-eager --max-model-len 2048`,
**When** `--relaunch --unset-args=--enforce-eager`,
**Then** `--enforce-eager` is removed but `--max-model-len 2048` is preserved.

## A23. Unset value-bearing flag

**Given** a previous launch with `--enforce-eager --max-model-len 2048`,
**When** `--relaunch --unset-args=--max-model-len`,
**Then** both `--max-model-len` and `2048` are removed but `--enforce-eager` is preserved.

## A24. JSON arg passthrough

**Given** `-- --additional-config '{"torchair_graph_config":{"enabled":false}}'` in extra args,
**When** `serve_start.py` launches,
**Then** the JSON double quotes survive the SSH + bash escaping layers and `vllm serve` receives valid JSON.

## A25. W8A8 quantization

**Given** a W8A8-quantized model path and `--quantization ascend` in extra args,
**When** `serve_start.py` launches with `--tp 4`,
**Then** the model loads and `/v1/chat/completions` returns valid responses.

## A26. MoE model support

**Given** a Mixture-of-Experts model (e.g. Qwen3.5-35B-A3B),
**When** `serve_start.py` launches with `--tp 8 --trust-remote-code`,
**Then** the model loads and `/v1/chat/completions` returns valid responses.

## A27. Chunked prefill

**When** `serve_start.py` launches with `--enable-chunked-prefill`,
**Then** the service starts successfully.

## A28. Prefix caching

**When** `serve_start.py` launches with `--enable-prefix-caching`,
**Then** the service starts successfully.
