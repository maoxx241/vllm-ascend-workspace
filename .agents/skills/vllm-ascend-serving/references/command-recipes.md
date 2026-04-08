# Command Recipes

## Fresh start with basic params

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a \
  --model /data/models/Qwen3-32B \
  --tp 4 \
  --devices 0,1,2,3
```

## Fresh start with extra vllm args

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a \
  --model /data/models/Qwen3-32B \
  --served-model-name qwen3-32b \
  --tp 4 \
  --devices 0,1,2,3 \
  --extra-env VLLM_USE_V1=1 \
  -- --max-model-len 4096 --gpu-memory-utilization 0.9
```

## Relaunch with same config (e.g. after code change)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a --relaunch
```

## Relaunch with extra debug env

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a --relaunch \
  --extra-env VLLM_LOGGING_LEVEL=DEBUG \
  --extra-env VLLM_TRACE_FUNCTION=1
```

## Relaunch and remove a previously set env

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a --relaunch \
  --unset-env VLLM_TRACE_FUNCTION
```

## Relaunch with a different model

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a --relaunch \
  --model /data/models/DeepSeek-V3 \
  --served-model-name deepseek-v3
```

## Relaunch and remove a previous vllm arg (value-bearing)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a --relaunch \
  --unset-args=--max-model-len
```

Note: use `=` syntax to prevent argparse from treating `--max-model-len` as a separate flag. This removes both `--max-model-len` and its value (e.g. `2048`).

## Relaunch and remove a boolean flag

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a --relaunch \
  --unset-args=--enforce-eager
```

Boolean flags like `--enforce-eager` are removed alone (the next token is not consumed).

## Relaunch skipping parity (when you know code hasn't changed)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a --relaunch --skip-parity
```

## Start with a forced port

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a \
  --model /data/models/Qwen3-32B \
  --tp 4 --port 8000
```

## Start with extended health timeout (for very large models)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a \
  --model /data/models/DeepSeek-V3-685B \
  --tp 8 \
  --health-timeout 1200
```

## Start with auto-selected devices (just specify tp)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a \
  --model /data/models/Qwen3-32B \
  --tp 4
```

The script probes NPUs, finds 4 free devices, and auto-selects them.

## Probe NPU availability before deciding

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py \
  --machine blue-a
```

This probes the **bare-metal host** (not the container) for cross-container NPU visibility. Example output:

```json
{
  "status": "ok",
  "machine": "blue-a",
  "total": 8,
  "devices": [0, 1, 2, 3, 4, 5, 6, 7],
  "busy": {"0": [{"pid": 12345, "owner": "root", "name": "python3"}],
           "1": [{"pid": 12345, "owner": "root", "name": "python3"}]},
  "hbm": {"0": 8192, "1": 8192, "2": 0, "3": 0},
  "free": [2, 3, 4, 5, 6, 7],
  "free_count": 6,
  "hbm_busy_threshold_mb": 4096
}
```

## Check status

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_status.py \
  --machine blue-a
```

## Stop gracefully

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py \
  --machine blue-a
```

## Force stop

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py \
  --machine blue-a --force
```

## Start with Ascend W8A8 quantization

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a \
  --model /data/models/Qwen3-32B-W8A8 \
  --tp 4 \
  -- --enforce-eager --max-model-len 4096 --quantization ascend --trust-remote-code
```

## Start an MoE model (all 8 cards)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a \
  --model /data/models/Qwen3.5-35B-A3B \
  --tp 8 \
  -- --enforce-eager --max-model-len 2048 --trust-remote-code
```

## Start with additional-config (JSON passthrough)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a \
  --model /data/models/Qwen3-32B \
  --tp 4 \
  -- --enforce-eager --additional-config '{"torchair_graph_config":{"enabled":false}}'
```

JSON double quotes are preserved through the SSH escaping layers.

## Start with chunked prefill

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a \
  --model /data/models/Qwen3-32B \
  --tp 4 \
  -- --enforce-eager --enable-chunked-prefill
```

## Start with prefix caching

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine blue-a \
  --model /data/models/Qwen3-32B \
  --tp 4 \
  -- --enforce-eager --enable-prefix-caching
```

## Rebuild custom CANN operators after parity sync

After `remote-code-parity` syncs tracked files, custom op build artifacts are missing. Rebuild them before launch:

```bash
ssh -p <container_ssh_port> root@<host_ip> 'bash -c "
  set +u; source /etc/profile.d/vaws-ascend-env.sh; set -u
  cd /vllm-workspace/vllm-ascend
  bash csrc/build_aclnn.sh /vllm-workspace/vllm-ascend ascend910b
"'
```

Note: if `numpy>=2.0` is installed, first downgrade: `pip3 install "numpy<2.0.0" -i https://pypi.tuna.tsinghua.edu.cn/simple`
