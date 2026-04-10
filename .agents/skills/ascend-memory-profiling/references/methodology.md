# Methodology: Ascend NPU Memory Profiling

## Core Principle

**msprof is the primary data source.** It observes memory allocations at the CANN runtime level, capturing components that PyTorch's allocator cannot see (HCCL communication buffers, CANN runtime internal allocations, system logging buffers). npu-smi provides hardware-level ground truth for total HBM usage. vLLM logs provide application-level attribution for weights and KV cache.

## Phased Collection

### Phase 0: Static Baseline

**Tool:** `npu-smi info`  
**When:** Before any vLLM process starts  
**What it measures:** Driver and base runtime memory that persists regardless of workload  
**Typical values:** ~2800-3000 MB per 910B4 device  
**Why needed:** Establishes the floor for delta calculations

### Phase 1: Service Startup with msprof

**Tool:** `msprof --application --sys-hardware-mem=on`  
**When:** Wraps the vLLM serve process from start to finish  
**What it captures:**
- `npu_module_mem_*.csv`: Per-component (APP, HCCL, RUNTIME, SLOG, etc.) memory timeline at configurable sampling frequency
- `npu_mem_*.csv`: Device-level and APP-level HBM timeline
- `op_summary_*.csv`: Operator execution data
- `communication_statistic_*.csv`: HCCL communication data

**Key insight:** msprof creates one PROF directory per process. With TP=4, you get 4 PROF directories, one per worker. Each contains device-specific data.

### Phase 2: Service Ready State

**Tool:** `npu-smi info` + vLLM startup log parsing  
**When:** After vLLM reports "Available routes are" (service ready)  
**What it measures:**
- Total HBM after model load + KV cache allocation
- vLLM self-reported: "Loading model weights took X GB"
- vLLM self-reported: "Available KV cache memory: X GiB"
- vLLM self-reported: "GPU KV cache size: N tokens"

### Phase 3: Under Inference Load

**Tool:** `npu-smi info` + inference request  
**When:** During active inference  
**What it measures:** Peak HBM including activations  
**Delta from Phase 2:** Activation memory estimate

### Phase 4-5: Stop and Export

**Tool:** Process termination + `msprof --export`  
**What it does:** Converts binary PROF data to readable CSV format

## Component Attribution Logic

### Fixed Overhead
```
fixed_overhead = npu_smi_phase0.HBM_Used
```
This includes driver, base CANN runtime, and system services.

### Model Weights
```
weights = vllm_log."Loading model weights took X GB"
theory  = sum(param_count × bytes_per_element) / TP_size
```
Cross-validation: `|weights - theory| / theory < 5%` is expected.

### KV Cache
```
kv_cache = vllm_log."Available KV cache memory: X GiB"
```
This is pre-allocated by the vLLM KV cache allocator. For MoE models, KV cache is shared across experts.

### HCCL Buffers
```
hccl = max(msprof.npu_module_mem where Component=HCCL)
```
Communication buffers for tensor parallel all-reduce operations. Scale with TP size.

### CANN Runtime
```
runtime = max(msprof.npu_module_mem where Component=RUNTIME)
```
CANN runtime's internal memory management overhead.

### Activations
```
activations = npu_smi_phase3.HBM_Used - npu_smi_phase2.HBM_Used
```
Transient memory for intermediate computations during inference.

### Unattributed
```
unattributed = npu_smi_ready.HBM_Used - sum(all_components)
```
Explicitly reported with percentage. Should be < 5% for a well-attributed profile.

## Cross-Validation Strategy

1. **npu-smi total vs component sum**: `|npu_smi - sum(components)| / npu_smi < 5%`
2. **msprof APP vs vLLM (weights+KV)**: `APP ≈ weights + KV + minor_overhead`
3. **Weights vs theory**: `|log_weight - theory_weight| / theory < 5%`
4. **msprof Device vs npu-smi**: Should be close but may differ by timing

## Known Behaviors

- **msprof wrapping adds overhead**: ~50-60s extra startup time due to sys-hardware-mem sampling initialization
- **npu-smi baseline varies**: The "fixed overhead" includes small transient allocations from other system services. Repeat measurement recommended.
- **MoE models (e.g., Qwen3.5-35B-A3B)**: With EP=TP, each device holds a fraction of experts. Weight size per device may be counter-intuitive for MoE.
- **expandable_segments**: vLLM sets `PYTORCH_NPU_ALLOC_CONF=expandable_segments:True`, meaning the PyTorch allocator may reserve more HBM than currently allocated. msprof APP tracks reserved (not just allocated).
