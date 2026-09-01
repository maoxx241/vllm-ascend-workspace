# Command Recipes

All serving commands are session-scoped. When run from inside a session worktree
(a directory with `.vaws-local/current-session.json`), the session is auto-resolved
and **no target flag is needed** — that is the primary form shown below. Outside a
worktree, add `--session-id <id>` (or `--session-file <path>`) explicitly.

The start workflow automatically consumes the local workspace identity. Check
it before launch with `python3 .agents/scripts/workspace_identity.py summary`.

## Fresh start with basic params

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --model /data/models/Qwen3-32B \
  --tp 4 \
  --devices 0,1,2,3
```

## Fresh start with an explicit session target

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --session-id pr123 \
  --model /data/models/Qwen3-32B \
  --tp 4
```

Both forms use the session container and write state under `.vaws-local/sessions/<session-id>/serving.json`.

## Fresh start with extra vllm args

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --model /data/models/Qwen3-32B \
  --served-model-name qwen3-32b \
  --tp 4 \
  --devices 0,1,2,3 \
  --extra-env VLLM_USE_V1=1 \
  -- --max-model-len 4096 --gpu-memory-utilization 0.9
```

## Relaunch with same config (e.g. after code change)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py --relaunch
```

## Relaunch with extra debug env

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --relaunch \
  --extra-env VLLM_LOGGING_LEVEL=DEBUG \
  --extra-env VLLM_TRACE_FUNCTION=1
```

## Relaunch and remove a previously set env

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --relaunch \
  --unset-env VLLM_TRACE_FUNCTION
```

## Relaunch with a different model

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --relaunch \
  --model /data/models/DeepSeek-V3 \
  --served-model-name deepseek-v3
```

## Relaunch and remove a previous vllm arg (value-bearing)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --relaunch \
  --unset-args=--max-model-len
```

Note: use `=` syntax to prevent argparse from treating `--max-model-len` as a separate flag. This removes both `--max-model-len` and its value (e.g. `2048`).

## Relaunch and remove a boolean flag

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --relaunch \
  --unset-args=--enforce-eager
```

Boolean flags like `--enforce-eager` are removed alone (the next token is not consumed).

## Relaunch skipping parity (when you know code hasn't changed)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --relaunch --skip-parity
```

## Start with a forced port

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --model /data/models/Qwen3-32B \
  --tp 4 --port 8000
```

Without `--port`, the service port is leased through the session lease mechanism.

## Start with extended health timeout (for very large models)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --model /data/models/DeepSeek-V3-685B \
  --tp 8 \
  --health-timeout 1200
```

## Start with lease-derived devices (just specify tp)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --model /data/models/Qwen3-32B \
  --tp 4
```

Without `--devices`, the launch uses the first 4 devices of the session's live NPU lease and verifies them free via the host probe. Free cards outside the lease are never auto-selected.

## Probe NPU availability before deciding

```bash
# Probe the session's base host (inside a session worktree)
python3 .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py

# Or with an explicit session target
python3 .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py \
  --session-id pr123
```

For resource-pool probing of a registered machine host (machine-management scope), `serve_probe_npus.py` alternatively accepts `--machine <alias-or-ip>` — the two surfaces are mutually exclusive:

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py \
  --machine blue-a
```

This probes the **bare-metal host** (not the container) for cross-container NPU visibility. Example output:

```json
{
  "status": "ok",
  "machine": "blue-a",
  "session_id": null,
  "collected_at": "2026-08-11T12:00:00Z",
  "devices": [0, 1, 2, 3, 4, 5, 6, 7],
  "busy": {
    "0": [{"kind": "process", "pid": 12345, "name": "python3"}],
    "1": [{"kind": "hbm_threshold", "hbm_used_mb": 8192, "threshold_mb": 4096}]
  },
  "hbm": {"0": {"used_mb": 3364, "total_mb": 65536}, "1": {"used_mb": 8192, "total_mb": 65536}},
  "free": [2, 3, 4, 5, 6, 7],
  "free_count": 6,
  "total": 8,
  "npu_smi_ok": true,
  "hbm_busy_threshold_mb": 4096
}
```

## Check status

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_status.py
```

Explicit session target:

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_status.py \
  --session-id pr123
```

## Stop gracefully

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py
```

Explicit session target:

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py \
  --session-id pr123
```

## Force stop

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py --force
```

## Start with Ascend W8A8 quantization

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --model /data/models/Qwen3-32B-W8A8 \
  --tp 4 \
  -- --enforce-eager --max-model-len 4096 --quantization ascend --trust-remote-code
```

## Start an MoE model (all 8 cards)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --model /data/models/Qwen3.5-35B-A3B \
  --tp 8 \
  -- --enforce-eager --max-model-len 2048 --trust-remote-code
```

## Start with additional-config (JSON passthrough)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --model /data/models/Qwen3-32B \
  --tp 4 \
  -- --enforce-eager --additional-config '{"torchair_graph_config":{"enabled":false}}'
```

JSON double quotes are preserved through the SSH escaping layers.

## Start with chunked prefill

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --model /data/models/Qwen3-32B \
  --tp 4 \
  -- --enforce-eager --enable-chunked-prefill
```

## Start with prefix caching

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --model /data/models/Qwen3-32B \
  --tp 4 \
  -- --enforce-eager --enable-prefix-caching
```

## Rebuild custom CANN operators after parity sync

After `remote-code-parity` syncs tracked files, custom op build artifacts are missing. Rebuild them before launch:

```bash
python3 .agents/scripts/remote_job_start.py \
  --session-id <session-id> \
  --kind build \
  --cwd /vllm-workspace/vllm-ascend \
  --command 'bash csrc/build_aclnn.sh /vllm-workspace/vllm-ascend ascend910b'
python3 .agents/scripts/remote_job_status.py --job-id <job-id>
python3 .agents/scripts/remote_job_tail.py --job-id <job-id> --lines 120
```

Note: if `numpy>=2.0` is installed, first downgrade through parity or use the same HuaweiCloud pip index: `pip3 install "numpy<2.0.0" -i https://repo.huaweicloud.com/repository/pypi/simple`
