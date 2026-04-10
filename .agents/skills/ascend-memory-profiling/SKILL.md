---
name: ascend-memory-profiling
description: Profile and attribute HBM memory usage on Ascend NPU for vLLM serving scenarios. Breaks down memory into fixed overhead, model weights, KV cache, HCCL buffers, activations, and runtime, with traceable evidence chains. Use for requests like "分析显存占用", "显存 profiling", "HBM 用了多少", "内存各部分拆分". Do not use for performance profiling (kernel timing, throughput), offline inference, or non-Ascend hardware.
---

# Ascend Memory Profiling

Collect and analyze HBM memory usage on Ascend NPU devices running vLLM serving workloads. Produces a structured breakdown of memory by component, with every value traceable to its data source.

## Use this skill when

- the user asks to profile or analyze GPU/NPU memory (显存) usage
- the user wants to understand what consumes HBM in a vLLM serving scenario
- the user asks "权重/KV cache/HCCL/激活各占多少显存"
- the user wants to verify memory allocation against theoretical expectations
- the user asks to compare memory usage across different configurations

## Do not use this skill when

- the task is performance profiling (kernel timing, bubble analysis) → use `ascend-profiling-anomaly`
- the task is starting/stopping a service without memory analysis → use `vllm-ascend-serving`
- the task involves non-Ascend hardware
- the task is offline (non-serving) inference only

## Data source priority

| Priority | Source | Role | Trustworthiness |
|----------|--------|------|-----------------|
| P0 | `msprof --application` wrapping | Full component breakdown (APP, HCCL, RUNTIME, SLOG) | Highest -- sees memory torch cannot manage |
| P1 | `npu-smi info` | Static baseline + phased delta | High -- hardware-level |
| P2 | vLLM startup logs | Weights, KV cache, num_gpu_blocks | Medium-high -- application-reported |
| P3 | `safetensors` file headers | Exact tensor shapes, dtypes, per-component breakdown | High -- byte-accurate |
| P4 | Model `config.json` | Theoretical weight calculation (fallback) | Reference only |

## Memory components

| Component | Source | How derived |
|-----------|--------|-------------|
| Fixed overhead (driver) | npu-smi Phase 0 | HBM_Used with no user process |
| Model weights | safetensors + vLLM logs | `weight_inspector.py` 解析文件头 → 按 TP/EP/DP 分片 → 与 vLLM `DeviceMemoryProfiler` 交叉验证 |
| KV cache | vLLM logs | "Available KV cache memory: X GiB" |
| ACL Graph 编译缓冲 | vLLM logs | "Graph capturing finished in X secs, took Y GiB" |
| HCCL buffers | msprof | npu_module_mem.csv Component=HCCL |
| CANN Runtime | msprof | npu_module_mem.csv Component=RUNTIME |
| Activations | npu-smi delta | HBM during inference minus HBM at idle |
| 未归因残差 | Residual | 所有已知组件加总后的余量 (有 msprof 时通常 < 200 MB) |

### Residual handling

All memory attribution is based on measured data — **no estimation or guessing** is performed.

- **With msprof**: HCCL, RUNTIME, SLOG are precise msprof measurements. Any remaining residual after all components is small (typically < 200 MB) and reported as "未归因残差".
- **Without msprof**: The residual is reported as "未归因 (缺少 msprof 数据)" with an explicit note that msprof collection is needed for a complete breakdown. No attempt is made to split the residual into sub-components.

## Workflow

**Always use the `vllm-ascend-serving` skill for service lifecycle management.** This profiling skill only collects and analyzes data — it attaches to a running service. **msprof wrapping is mandatory** for a complete, traceable memory breakdown.

### Step 0: Check msprof availability

Before starting the service, verify that msprof is available on the remote machine:

```bash
python3 -c "
import sys; sys.path.insert(0, '.agents/skills/ascend-memory-profiling/scripts')
from _common import resolve_machine, endpoint_from_machine, check_msprof_available
ep = endpoint_from_machine(resolve_machine('<alias>'))
print(check_msprof_available(ep))
"
```

If this fails, stop and fix the remote environment before proceeding.

### Step 1: Prepare the service with msprof (via `vllm-ascend-serving`)

Upload the msprof wrapper, then start the service with `--wrap-script`:

```bash
# Upload msprof wrapper to remote
python3 -c "
import sys; sys.path.insert(0, '.agents/skills/ascend-memory-profiling/scripts')
from _common import resolve_machine, endpoint_from_machine, upload_msprof_wrapper
ep = endpoint_from_machine(resolve_machine('<alias>'))
print(upload_msprof_wrapper(ep, mem_freq=50))
"
# Start service with msprof wrapping
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --machine <alias> --model <path> --tp <N> \
  --wrap-script /tmp/_vaws_msprof_wrap.sh \
  [-- --speculative-config '...' --compilation-config '...' ...]
```

Note: `upload_msprof_wrapper` internally calls `check_msprof_available` as a safety net.

For baseline npu-smi data, collect `npu-smi info` **before** starting the service.

### Step 2: Collect memory data (attach mode)

```bash
python3 .agents/skills/ascend-memory-profiling/scripts/mem_collect.py \
  --machine <alias-or-ip> \
  --attach \
  [--baseline-from <previous-run-dir>] \
  [--tag <experiment-name>]
```

What happens:
- Reads `.vaws-local/serving/<alias>.json` to discover port, PID, model path, tp/dp, extra args
- Auto-extracts `--speculative-config`, `--compilation-config`, etc. from the serving state
- Collects npu-smi snapshot, vLLM logs (from serving's runtime dir), weight manifest
- Sends inference request to collect activation delta
- **Does NOT** start or stop the service

### Step 3: Stop service (via `vllm-ascend-serving`)

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py --machine <alias>
```

### Step 4: Collect msprof data

After stop, run `mem_collect --attach` again. It detects the msprof wrapper from the serving state, automatically runs `msprof --export`, and downloads the resulting CSVs:

```bash
python3 .agents/skills/ascend-memory-profiling/scripts/mem_collect.py \
  --machine <alias> --attach --tag <same-experiment-name>
```

This works because attach mode accepts stopped services — it skips health check and inference, and focuses on collecting msprof CSVs that are now available.

### Step 5: Analyze and generate report

```bash
python3 .agents/skills/ascend-memory-profiling/scripts/mem_analyze.py \
  .vaws-local/memory-profiling/<run-dir>/
```

### Baseline strategy

| Situation | How to get baseline |
|-----------|-------------------|
| Fresh profiling | Collect `npu-smi info` before `serve_start`, save to file, use `--baseline-from` |
| Repeat profiling on same machine | Reuse baseline from a previous run via `--baseline-from <old-run-dir>` |
| Quick analysis (no baseline needed) | Omit `--baseline-from` — report shows "固定开销" as 0 with note |

### Fallback: Standalone mode

When the serving skill is unavailable (e.g. bootstrap scenario), `mem_collect.py` can manage the service internally. This is a **fallback** — prefer the serving skill workflow above.

```bash
python3 .agents/skills/ascend-memory-profiling/scripts/mem_collect.py \
  --machine <alias-or-ip> \
  --model <remote-weight-path> \
  --tp <N> [--dp <N>] [--tag <name>] \
  [--speculative-config '...'] [--compilation-config '...'] ...
```

This runs all phases internally: baseline → start (with msprof) → health check → snapshot → inference → stop → msprof export. msprof is always enabled in standalone mode; a pre-flight check verifies msprof availability before starting.

### Standalone: Analyze and generate report

```bash
python3 .agents/skills/ascend-memory-profiling/scripts/mem_analyze.py \
  .vaws-local/memory-profiling/<run-dir>/
```

Outputs:
- `report.txt` -- human-readable report with evidence chains
- `report.json` -- machine-readable structured report

### Example output (S2: MoE 35B, TP=4, DP=2, MTP=3, FULL_DECODE_ONLY, with msprof)

```
[Device 0] 总 HBM: 32.00 GiB | 已用: 27.42 GiB

组件                           |    占用 (MB) |   占用 (GiB) |     占比 | 主数据源
--------------------------------------------------------------------------------------------------------------
固定开销 (driver/runtime)        |     2930.0 |      2.861 |  10.4% | npu-smi Phase 0 (baseline)
模型权重                         |     9692.6 |      9.465 |  34.5% | safetensors 文件头精确计算
KV Cache 预留                  |    14080.0 |     13.750 |  50.2% | vLLM 日志
ACL Graph 编译缓冲               |      501.8 |      0.490 |   1.8% | vLLM 日志
HCCL 缓冲                      |      597.7 |      0.584 |   2.1% | msprof npu_module_mem
CANN Runtime                   |      125.3 |      0.122 |   0.4% | msprof npu_module_mem
激活峰值                         |       72.0 |      0.070 |   0.3% | npu-smi delta
  └ 未归因残差                    |       74.7 |      0.073 |   0.3% | 残差

[交叉验证]
  npu-smi 已用:          28,074 MB
  组件加总:              27,999.3 MB
  未归因:                74.7 MB (0.3%)
  msprof APP:            27,500 MB
```

## Interaction with other skills

| Skill | Interaction |
|-------|-------------|
| `vllm-ascend-serving` | **Service lifecycle**: Use `serve_start.py` to start (with `--wrap-script` for msprof), `serve_stop.py` to stop. This skill reads serving state from `.vaws-local/serving/<alias>.json`. The serving skill is agnostic to msprof — it only knows about the wrapper script. |
| `machine-management` | **SSH endpoint resolution**: Both skills share the machine inventory via `inventory` from `.agents/lib/`. |
| `remote-code-parity` | **Automatic via serving**: The serving skill calls parity sync before service start. |

## Critical rules

- **Service lifecycle belongs to `vllm-ascend-serving`** — this skill only collects and analyzes data.
- In attach mode, `mem_collect` will **never** start or stop the service itself. Service stop is done by the agent calling `serve_stop.py` directly. After stop, `mem_collect --attach` can be run again to export and collect msprof data.
- In standalone mode (fallback), the service is started and stopped within the profiling run.
- `msprof` export (8 卡) 可能需要几分钟。
- Keep collected data under `.vaws-local/memory-profiling/` only (untracked).

## Weight analysis methodology

The skill uses `weight_inspector.py` to parse safetensors file headers on the remote machine, extracting exact tensor names, shapes, dtypes, and byte sizes. This provides a **byte-accurate** breakdown of model weights by component.

### How it works

1. **Parse safetensors headers**: Each `.safetensors` file starts with a JSON header containing tensor metadata (name, shape, dtype, offsets). No weight data is read — only the header (typically a few KB per file).

2. **Classify tensors**: Each tensor is categorized by name pattern:
   - `embed_tokens` → Embedding
   - `self_attn.{q,k,v,o}_proj` → Full Attention
   - `linear_attn.{in_proj_qkv,in_proj_z,out_proj,conv1d,...}` → Linear/Mamba Attention
   - `mlp.experts.*` → MoE Experts
   - `mlp.shared_expert.*` → MoE Shared Expert
   - `visual.*` → Vision Encoder
   - `mtp.*` → MTP (Multi-Token Prediction)
   - `norm`, `layernorm` → Layer Norms

3. **Determine shard strategy**: Each tensor is assigned a parallelism strategy:
   - `col_parallel` / `row_parallel` → divided by TP
   - `expert_parallel` → divided by EP (= TP × DP when EP enabled)
   - `replicated` → full copy on each device (norms, gates, small params)

4. **Calculate per-device weight**: Sum up per-device bytes across all tensors.

5. **Cross-validate with vLLM**: Compare safetensors-derived per-device value with vLLM's `DeviceMemoryProfiler` measurement. Expected differences:
   - **MTP embedding/lm_head sharing**: MTP reuses base model's embeddings (reduces actual memory)
   - **F32 → BF16 conversion**: Some small parameters (A_log, dt_bias) stored as F32 but may load as BF16
   - **Vision encoder in text-only mode**: Still loaded for multimodal architectures

### When the script isn't sufficient

If the safetensors-based analysis shows unexpected results, the agent should:
1. Check `weight_manifest.json` for unclassified ("other") tensors
2. Inspect vLLM's model implementation (`load_weights`) for weight sharing or skipping logic
3. Compare `model.named_parameters()` output on the remote machine with the safetensors manifest
4. Check if `enable_ep_weight_filter` affects loading behavior

## Limitations

- Activation measurement relies on npu-smi delta between idle and inference states. This captures peak but not fine-grained activation lifetime.
- `torch_npu.profiler` via vLLM's `/start_profile`/`/stop_profile` endpoints currently does not produce device-side data (device_0/data is empty). Use msprof wrapping instead.
- msprof wrapping profiles the main process only. TP worker processes are separate -- each gets its own PROF directory with per-device data.
- msprof export for 8-card runs may take several minutes. The timeout is set to 1800s.

## References

- `references/methodology.md` -- Detailed methodology and data source descriptions
- `references/msprof_fields.md` -- msprof CSV field reference
