# msprof Memory CSV Field Reference

Based on Ascend CANN documentation and empirical verification.

## 1. `npu_module_mem_*.csv` -- Component-Level Memory

Automatically collected when `--sys-hardware-mem=on` is passed to msprof or when using `torch_npu.profiler`. Records per-component memory occupation over time.

| Field | Description | Unit |
|-------|-------------|------|
| Device_id | Logical device index | -- |
| Component | Module name (see table below) | -- |
| Timestamp(us) | Sample timestamp | microseconds |
| Total Reserved(KB) | Memory reserved by this component | KB (PROF dir) or MB (ASCEND_PROFILER_OUTPUT) |
| Device | Device type and ID | e.g., "NPU:0" |

### Key Components

| Component | What it represents | Typical range (910B4) |
|-----------|-------------------|----------------------|
| APP | All application-level memory (PyTorch allocator + GE) | 10-25 GB |
| HCCL | Communication buffers for collective ops (all-reduce, etc.) | 200-500 MB |
| RUNTIME | CANN runtime internal allocations | 50-100 MB |
| SLOG | System logging buffers | 100-150 MB |
| GE | Graph Engine internal memory | Variable |
| FE | Frontend memory | Small |
| DEVMM | Device Memory Management | Small |
| Others | AICPU, CCE, TBE, TS, etc. | Usually 0 |

## 2. `npu_mem_*.csv` -- Device & APP Timeline

Records overall device-level and application-level HBM usage over time.

| Field | Description | Unit |
|-------|-------------|------|
| Device_id | Logical device index | -- |
| event | "Device" (total HBM) or "APP" (application only) | -- |
| ddr(KB) | DDR memory | KB |
| hbm(KB) | HBM memory | KB |
| memory(KB) | Total memory (usually = hbm) | KB |
| timestamp(us) | Sample timestamp | microseconds |

### Usage

- `event=Device` rows show total HBM including driver/runtime
- `event=APP` rows show only application-allocated memory
- `Device - APP` ≈ system overhead (driver, runtime, HCCL, SLOG)

## 3. `memory_record.csv` -- PTA/GE Allocation Timeline

Requires `profile_memory=True` in `torch_npu.profiler`. Records PyTorch Allocator (PTA) and Graph Engine (GE) allocation events.

| Field | Description | Unit |
|-------|-------------|------|
| Component | PTA / GE / PTA+GE (operator-level) / APP (process-level) | -- |
| Timestamp(us) | Allocation start time | microseconds |
| Total Allocated(MB) | Total memory allocated at this point | MB |
| Total Reserved(MB) | Total memory reserved at this point | MB |
| Total Active(MB) | Total active memory (including reused) | MB |
| Stream Ptr | AscendCL stream memory address | -- |
| Device Type | Device type and ID | e.g., "NPU:0" |

### Notes
- APP rows are sampled at regular intervals (process-level snapshot)
- PTA rows appear at each allocation/deallocation event
- PTA+GE rows combine both components for a unified view
- `Reserved > Allocated` is normal due to PyTorch's caching allocator

## 4. `operator_memory.csv` -- Per-Operator Memory

Requires `profile_memory=True`. Records memory lifecycle for each operator.

| Field | Description | Unit |
|-------|-------------|------|
| Name | Operator name (aten::* = PTA, cann::* = GE) | -- |
| Size(KB) | Memory allocated by this operator | KB |
| Allocation Time(us) | When memory was allocated | microseconds |
| Release Time(us) | When memory was released (empty if not released) | microseconds |
| Duration(us) | How long memory was held | microseconds |
| Allocation Total Allocated(MB) | Global allocated total at allocation time | MB |
| Allocation Total Reserved(MB) | Global reserved total at allocation time | MB |
| Device Type | Device type and ID | -- |

### Key patterns
- `aten::empty` with large Size → weight tensor allocation
- `aten::empty` without Release Time → permanent allocation (likely weights or KV cache)
- `aten::matmul` with small Size → activation tensors (transient)

## Collection Method Comparison

| Feature | msprof --application | torch_npu.profiler | npu-smi |
|---------|---------------------|-------------------|---------|
| npu_module_mem | ✓ (auto) | ✓ (auto) | ✗ |
| npu_mem | ✓ (auto) | ✓ (auto) | ✗ |
| memory_record | ✗ | ✓ (needs profile_memory) | ✗ |
| operator_memory | ✗ | ✓ (needs profile_memory) | ✗ |
| Component breakdown | ✓ (HCCL, RUNTIME, etc.) | ✓ (60+ components) | ✗ |
| Full lifecycle coverage | ✓ | Depends on schedule | ✗ |
| Multi-process (TP>1) | ✓ (per-process PROF) | Depends on integration | ✓ |
| Total HBM | Via npu_mem | Via npu_mem | ✓ |
| Overhead | Moderate | Moderate | Minimal |
