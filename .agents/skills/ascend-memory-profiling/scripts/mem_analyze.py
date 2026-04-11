#!/usr/bin/env python3
"""
Ascend Memory Profiling -- Analysis & Report Generator.

Reads the data collected by mem_collect.py and produces a structured memory
breakdown report with cross-validation and evidence chains.

Input: path to a collection run directory (containing manifest.json)
Output: printed report + saved report.json / report.txt
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class MemoryComponent:
    name: str
    value_mb: float
    source: str
    evidence: str = ""
    corroboration: str = ""
    delta_pct: float = 0.0


@dataclass
class DeviceReport:
    device_id: int
    total_hbm_mb: float
    used_hbm_mb: float
    components: list[MemoryComponent] = field(default_factory=list)
    cross_validation: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# npu-smi parsing
# ---------------------------------------------------------------------------

def parse_npu_smi_hbm(data: dict[str, dict]) -> dict[int, dict]:
    """Parse npu-smi HBM data from manifest."""
    result = {}
    for dev_str, info in data.items():
        dev_id = int(dev_str) if isinstance(dev_str, str) and dev_str.isdigit() else dev_str
        result[dev_id] = info
    return result


# ---------------------------------------------------------------------------
# vLLM log parsing
# ---------------------------------------------------------------------------

_WEIGHT_RE = re.compile(r"Loading model weights took\s+([\d.]+)\s*G[Bi]")
_KV_AVAIL_RE = re.compile(r"Available KV cache memory:\s+([\d.]+)\s*GiB")
_KV_TOKENS_RE = re.compile(r"GPU KV cache size:\s+([\d,]+)\s*tokens")
_WEIGHT_LOAD_TIME_RE = re.compile(r"Loading weights took\s+([\d.]+)\s*seconds")
_GRAPH_CAPTURE_RE = re.compile(r"Graph capturing finished in (\d+) secs, took ([\d.]+) GiB")
_COMPILE_WARMUP_RE = re.compile(r"torch\.compile and initial profiling/warmup run together took ([\d.]+) s")
_ENCODER_CACHE_RE = re.compile(r"Encoder cache will be initialized with a budget of (\d+) tokens")
_ACL_GRAPH_SIZES_RE = re.compile(r"with (\d+) sizes")


def parse_vllm_logs(log_text: str) -> dict[str, Any]:
    """Extract memory-related info from vLLM startup logs."""
    info: dict[str, Any] = {}
    graph_capture_gibs: list[float] = []

    for line in log_text.splitlines():
        m = _WEIGHT_RE.search(line)
        if m:
            info["weights_gb"] = float(m.group(1))

        m = _KV_AVAIL_RE.search(line)
        if m:
            info["kv_cache_available_gib"] = float(m.group(1))

        m = _KV_TOKENS_RE.search(line)
        if m:
            info["kv_cache_tokens"] = int(m.group(1).replace(",", ""))

        m = _WEIGHT_LOAD_TIME_RE.search(line)
        if m:
            info["weight_load_seconds"] = float(m.group(1))

        m = _GRAPH_CAPTURE_RE.search(line)
        if m:
            graph_capture_gibs.append(float(m.group(2)))

        m = _COMPILE_WARMUP_RE.search(line)
        if m and "compile_warmup_seconds" not in info:
            info["compile_warmup_seconds"] = float(m.group(1))

        m = _ENCODER_CACHE_RE.search(line)
        if m:
            info["encoder_cache_tokens"] = int(m.group(1))

        m = _ACL_GRAPH_SIZES_RE.search(line)
        if m and "acl_graph_sizes_count" not in info:
            info["acl_graph_sizes_count"] = int(m.group(1))

        if "gpu_memory_utilization" in line:
            m2 = re.search(r"gpu_memory_utilization['\"]?\s*[:=]\s*([\d.]+)", line)
            if m2:
                info["gpu_memory_utilization"] = float(m2.group(1))

        if "num_gpu_blocks" in line:
            m2 = re.search(r"num_gpu_blocks\s*[:=]\s*(\d+)", line)
            if m2:
                info["num_gpu_blocks"] = int(m2.group(1))

    if graph_capture_gibs:
        info["graph_capture_gib"] = max(graph_capture_gibs)
        info["graph_capture_count"] = len(graph_capture_gibs)

    return info


# ---------------------------------------------------------------------------
# msprof CSV parsing
# ---------------------------------------------------------------------------

def parse_npu_module_mem_csv(csv_path: Path) -> dict[str, float]:
    """Parse npu_module_mem CSV, return {component: max_reserved_mb}."""
    components: dict[str, float] = {}
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            comp = row.get("Component", "").strip()
            unit_key = "Total Reserved(MB)"
            if unit_key not in row:
                unit_key = "Total Reserved(KB)"
            raw = row.get(unit_key, "0").strip()
            try:
                val = float(raw)
            except ValueError:
                continue
            if "KB" in unit_key:
                val /= 1024.0
            if val > components.get(comp, 0):
                components[comp] = val
    return components


def parse_npu_mem_csv(csv_path: Path) -> dict[str, list[dict]]:
    """Parse npu_mem CSV, return {event: [{timestamp, hbm_kb}]}."""
    events: dict[str, list[dict]] = {}
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            event = row.get("event", "").strip()
            hbm = row.get("hbm(KB)", "0").strip()
            ts = row.get("timestamp(us)", "0").strip()
            try:
                hbm_val = float(hbm)
                ts_val = float(ts)
            except ValueError:
                continue
            events.setdefault(event, []).append({"timestamp_us": ts_val, "hbm_kb": hbm_val})
    return events


# ---------------------------------------------------------------------------
# Safetensors-based precise weight analysis
# ---------------------------------------------------------------------------

_SHARD_COL = "col_parallel"
_SHARD_ROW = "row_parallel"
_SHARD_EP = "expert_parallel"
_SHARD_REPL = "replicated"

_CATEGORY_DISPLAY = {
    "embedding": "Embedding",
    "lm_head": "LM Head",
    "attn_q": "Attention Q",
    "attn_k": "Attention K",
    "attn_v": "Attention V",
    "attn_o": "Attention O",
    "attn_conv": "Attention Conv",
    "attn_kv_compress": "Attention KV Compress",
    "attn_other": "Attention Other",
    "linear_attn_qkv": "Linear Attn QKV",
    "linear_attn_proj": "Linear Attn Proj (Z/O)",
    "linear_attn_conv": "Linear Attn Conv1d",
    "linear_attn_param": "Linear Attn Param",
    "ffn": "FFN (Dense)",
    "moe_expert": "MoE Experts",
    "moe_shared_expert": "MoE Shared Expert",
    "moe_gate": "MoE Router Gate",
    "moe_other": "MoE Other",
    "norm": "LayerNorm / RMSNorm",
    "vision": "Vision Encoder",
    "mtp_expert": "MTP Experts",
    "mtp_shared_expert": "MTP Shared Expert",
    "mtp_gate": "MTP Router Gate",
    "mtp_attn": "MTP Attention",
    "mtp_fc": "MTP FC Proj",
    "mtp_norm": "MTP Norm",
    "mtp_other": "MTP Other",
    "quant_param": "Quant Scales/ZP",
    "other": "Other",
}

_GROUP_ORDER = [
    "embedding", "lm_head",
    "attn_q", "attn_k", "attn_v", "attn_o",
    "attn_conv", "attn_kv_compress", "attn_other",
    "linear_attn_qkv", "linear_attn_proj", "linear_attn_conv", "linear_attn_param",
    "ffn",
    "moe_expert", "moe_shared_expert", "moe_gate", "moe_other",
    "norm", "vision",
    "mtp_expert", "mtp_shared_expert", "mtp_gate", "mtp_attn", "mtp_fc", "mtp_norm", "mtp_other",
    "quant_param", "other",
]


def compute_weight_from_manifest(
    weight_manifest: dict,
    tp: int,
    dp: int = 1,
    enable_ep: bool = False,
) -> dict[str, Any]:
    """Compute per-device weight size from safetensors manifest.

    Returns a dict with total bytes, per-device bytes, and per-category breakdown.
    """
    if not weight_manifest or "tensors" not in weight_manifest:
        return {}

    ep = tp * dp if enable_ep else tp
    tensors = weight_manifest["tensors"]

    total_bytes = 0
    per_device_bytes = 0
    categories: dict[str, dict] = {}

    for t in tensors:
        cat = t.get("category", "other")
        strategy = t.get("shard_strategy", _SHARD_REPL)
        bsz = t.get("byte_size", 0)

        total_bytes += bsz

        if strategy in (_SHARD_COL, _SHARD_ROW):
            dev_bytes = bsz / tp
        elif strategy == _SHARD_EP:
            dev_bytes = bsz / ep
        else:
            dev_bytes = bsz

        per_device_bytes += dev_bytes

        if cat not in categories:
            categories[cat] = {
                "total_bytes": 0,
                "per_device_bytes": 0.0,
                "tensor_count": 0,
                "dtypes": set(),
                "shard_strategy": strategy,
            }
        categories[cat]["total_bytes"] += bsz
        categories[cat]["per_device_bytes"] += dev_bytes
        categories[cat]["tensor_count"] += t.get("numel", 0)
        categories[cat]["dtypes"].add(t.get("dtype", "?"))

    for cat in categories:
        categories[cat]["dtypes"] = sorted(categories[cat]["dtypes"])

    sorted_cats = []
    for key in _GROUP_ORDER:
        if key in categories:
            c = categories[key]
            sorted_cats.append({
                "category": key,
                "display_name": _CATEGORY_DISPLAY.get(key, key),
                "total_bytes": c["total_bytes"],
                "total_gib": round(c["total_bytes"] / (1024**3), 4),
                "per_device_bytes": c["per_device_bytes"],
                "per_device_gib": round(c["per_device_bytes"] / (1024**3), 4),
                "dtypes": c["dtypes"],
            })

    return {
        "source": "safetensors_manifest",
        "total_bytes": total_bytes,
        "total_gib": round(total_bytes / (1024**3), 4),
        "per_device_bytes": per_device_bytes,
        "per_device_gib": round(per_device_bytes / (1024**3), 4),
        "tp": tp,
        "dp": dp,
        "ep": ep,
        "categories": sorted_cats,
    }


def format_weight_breakdown(weight_info: dict, vllm_gb: float = 0) -> list[str]:
    """Format the per-category weight breakdown as text lines."""
    lines = []
    lines.append("[权重精确分析] (数据源: safetensors 文件头)")
    per_dev_gib = weight_info['per_device_gib']
    per_dev_gb = per_dev_gib * 1024**3 / 10**9
    lines.append(f"  总权重(文件): {weight_info['total_gib']:.4f} GiB | "
                 f"每设备(理论分片): {per_dev_gib:.4f} GiB ({per_dev_gb:.4f} GB)")
    lines.append(f"  分片策略: TP={weight_info['tp']}, DP={weight_info['dp']}, "
                 f"EP={weight_info['ep']}")
    if vllm_gb:
        vllm_gib = vllm_gb * 10**9 / 1024**3
        diff_gib = per_dev_gib - vllm_gib
        diff_pct = abs(diff_gib) / vllm_gib * 100 if vllm_gib else 0
        lines.append(f"  vLLM 实际加载: {vllm_gb:.4f} GB ({vllm_gib:.4f} GiB)")
        sign = "+" if diff_gib > 0 else ""
        lines.append(f"  差值(safetensors vs vLLM): {sign}{diff_gib:.4f} GiB ({diff_pct:.1f}%)")
        if diff_gib > 0.1:
            lines.append("  说明: safetensors 高于 vLLM 实际值，可能原因:")
            lines.append("    - MTP 模块复用 base model 的 embed_tokens/lm_head (共享内存)")
            lines.append("    - 部分 F32 参数在加载时被转为 BF16")
            lines.append("    - vision encoder 在纯文本推理下可能未加载")
    lines.append("")
    lines.append(f"  {'组件':<28} | {'总量 (GiB)':>10} | {'每设备 (GiB)':>12} | {'占比':>6} | dtypes")
    lines.append("  " + "-" * 90)

    total_dev = weight_info["per_device_bytes"]
    for cat in weight_info.get("categories", []):
        pct = cat["per_device_bytes"] / total_dev * 100 if total_dev else 0
        lines.append(
            f"  {cat['display_name']:<28} | {cat['total_gib']:>10.4f} | "
            f"{cat['per_device_gib']:>12.4f} | {pct:>5.1f}% | {', '.join(cat['dtypes'])}"
        )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Model config / fallback theoretical weight calculation
# ---------------------------------------------------------------------------

DTYPE_BYTES = {"bfloat16": 2, "float16": 2, "float32": 4, "float8": 1, "int8": 1, "int4": 0.5}


def _estimate_vision_params(config: dict) -> int:
    """Estimate parameter count for the vision encoder (ViT)."""
    vc = config.get("vision_config")
    if not vc or not isinstance(vc, dict):
        return 0
    hidden = vc.get("hidden_size", 0)
    inter = vc.get("intermediate_size", 0)
    depth = vc.get("depth", 0)
    patch = vc.get("patch_size", 16)
    in_ch = vc.get("in_channels", 3)
    temp_patch = vc.get("temporal_patch_size", 2)
    out_hidden = vc.get("out_hidden_size", hidden)
    num_pos = vc.get("num_position_embeddings", 0)

    if not (hidden and depth):
        return 0

    patch_embed = in_ch * temp_patch * patch * patch * hidden
    pos_embed = num_pos * hidden if num_pos else 0

    per_block = (
        hidden * 3 * hidden  # QKV
        + hidden * hidden     # proj
        + 2 * hidden * inter  # MLP (fc1 + fc2)
        + 4 * hidden          # norms
    )
    merger = out_hidden * hidden * 4 if out_hidden != hidden else 0
    return patch_embed + pos_embed + depth * per_block + merger


def _estimate_mtp_params(tc: dict) -> int:
    """Estimate parameter count for MTP (multi-token prediction) heads."""
    mtp_layers = tc.get("mtp_num_hidden_layers", 0)
    if not mtp_layers:
        return 0
    hidden = tc.get("hidden_size", 0)
    intermediate = tc.get("intermediate_size") or (hidden * 4)
    vocab_size = tc.get("vocab_size", 0)
    use_dedicated_embed = tc.get("mtp_use_dedicated_embeddings", False)

    per_mtp_layer = (
        hidden * hidden  # proj
        + 3 * hidden * intermediate  # FFN
        + 4 * hidden  # norms
    )
    mtp_lm_head = vocab_size * hidden if not tc.get("tie_word_embeddings", True) else 0
    mtp_embed = vocab_size * hidden if use_dedicated_embed else 0
    return mtp_layers * (per_mtp_layer + mtp_lm_head + mtp_embed)


def estimate_weight_size(config: dict, tp: int, dp: int = 1) -> dict[str, Any]:
    """Estimate theoretical model weight size from config.json."""
    tc = config.get("text_config", config)
    hidden = tc.get("hidden_size", 0)
    num_layers = tc.get("num_hidden_layers", 0)
    vocab_size = tc.get("vocab_size", 0)
    intermediate = tc.get("intermediate_size") or 0
    moe_intermediate = tc.get("moe_intermediate_size") or 0
    shared_expert_intermediate = tc.get("shared_expert_intermediate_size") or 0
    num_experts = tc.get("num_experts") or 0
    num_kv_heads = tc.get("num_key_value_heads") or 0
    num_attn_heads = tc.get("num_attention_heads") or 0
    head_dim = tc.get("head_dim") or (hidden // num_attn_heads if num_attn_heads else 0)
    dtype = tc.get("dtype", config.get("torch_dtype", "bfloat16"))
    bpe = DTYPE_BYTES.get(dtype, 2)

    if not (hidden and num_layers and vocab_size):
        return {"error": "missing config fields", "dtype": dtype}

    tie = tc.get("tie_word_embeddings", config.get("tie_word_embeddings", False))
    embed_params = vocab_size * hidden
    lm_head_params = 0 if tie else vocab_size * hidden

    # Attention -- handle hybrid linear/full attention
    linear_key_head_dim = tc.get("linear_key_head_dim", 0)
    linear_num_kv_heads = tc.get("linear_num_key_heads", 0)
    layer_types = tc.get("layer_types", [])

    def _attn_params(layer_type: str = "full_attention") -> int:
        if layer_type == "linear_attention" and linear_key_head_dim and linear_num_kv_heads:
            lv_dim = tc.get("linear_value_head_dim", linear_key_head_dim)
            return (
                hidden * num_attn_heads * head_dim  # Q
                + hidden * linear_num_kv_heads * linear_key_head_dim  # K
                + hidden * linear_num_kv_heads * lv_dim  # V
                + num_attn_heads * head_dim * hidden  # O
                + hidden * tc.get("linear_conv_kernel_dim", 4)  # conv
            )
        return (
            hidden * num_attn_heads * head_dim
            + hidden * num_kv_heads * head_dim
            + hidden * num_kv_heads * head_dim
            + num_attn_heads * head_dim * hidden
        )

    total_attn_params = 0
    if layer_types:
        for lt in layer_types:
            total_attn_params += _attn_params(lt)
    else:
        total_attn_params = num_layers * _attn_params()

    norm_params_per_layer = hidden * 4
    total_norm_params = num_layers * norm_params_per_layer

    if num_experts and num_experts > 1:
        expert_ff = moe_intermediate or intermediate
        expert_params_per_layer = num_experts * 3 * hidden * expert_ff
        shared_params_per_layer = 3 * hidden * shared_expert_intermediate if shared_expert_intermediate else 0
        gate_params_per_layer = hidden * num_experts
        ff_params_per_layer = expert_params_per_layer + shared_params_per_layer + gate_params_per_layer
    else:
        ff_intermediate = intermediate or (hidden * 4)
        ff_params_per_layer = 3 * hidden * ff_intermediate

    total_ff_params = num_layers * ff_params_per_layer

    vision_params = _estimate_vision_params(config)
    mtp_params = _estimate_mtp_params(tc)

    total_params = (
        embed_params + lm_head_params
        + total_attn_params + total_ff_params + total_norm_params
        + vision_params + mtp_params
    )
    total_bytes = total_params * bpe

    # Per-device calculation
    if num_experts and num_experts > 1 and tp > 1:
        ep = tp * dp
        attn_bytes = total_attn_params * bpe / tp
        expert_bytes = num_layers * num_experts * 3 * hidden * (moe_intermediate or intermediate) * bpe / ep
        shared_bytes = num_layers * 3 * hidden * shared_expert_intermediate * bpe / tp if shared_expert_intermediate else 0
        gate_bytes = num_layers * hidden * num_experts * bpe
        embed_bytes = (embed_params + lm_head_params) * bpe / tp
        norm_bytes = total_norm_params * bpe
        vision_bytes = vision_params * bpe
        mtp_bytes = mtp_params * bpe
        per_device_bytes = attn_bytes + expert_bytes + shared_bytes + gate_bytes + embed_bytes + norm_bytes + vision_bytes + mtp_bytes
    else:
        per_device_bytes = total_bytes / tp if tp else total_bytes

    return {
        "total_params_estimate": total_params,
        "dtype": dtype,
        "bytes_per_element": bpe,
        "total_weight_bytes": total_bytes,
        "per_device_weight_bytes": per_device_bytes,
        "per_device_weight_gib": per_device_bytes / (1024 ** 3),
        "tp": tp,
        "is_moe": bool(num_experts and num_experts > 1),
        "num_experts": num_experts,
        "vision_params": vision_params,
        "mtp_params": mtp_params,
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_device_report(
    dev_id: int,
    baseline_hbm: dict,
    after_ready_hbm: dict,
    after_infer_hbm: dict,
    msprof_components: dict[str, float],
    vllm_info: dict,
    weight_theory: dict,
    weight_precise: dict,
    tp: int,
    msprof_scope: str = "process-level",
) -> DeviceReport:
    """Generate a memory breakdown report for a single device."""
    baseline = baseline_hbm.get(dev_id, {})
    ready = after_ready_hbm.get(dev_id, {})
    infer = after_infer_hbm.get(dev_id, {})

    total_mb = ready.get("total_mb", 32768)
    used_mb = ready.get("used_mb", 0)
    baseline_mb = baseline.get("used_mb", 0)

    report = DeviceReport(
        device_id=dev_id,
        total_hbm_mb=total_mb,
        used_hbm_mb=used_mb,
    )

    # Fixed overhead (driver/runtime base)
    if baseline_mb > 0:
        report.components.append(MemoryComponent(
            name="固定开销 (driver/runtime)",
            value_mb=baseline_mb,
            source="npu-smi Phase 0 (baseline, 无用户进程)",
            evidence=f"baseline_npu_smi.txt: Device {dev_id} HBM_Used = {baseline_mb} MB",
        ))
    else:
        report.components.append(MemoryComponent(
            name="固定开销 (driver/runtime)",
            value_mb=0,
            source="不可用 (attach 模式无基线数据)",
            evidence="服务已在运行，无法获取空载基线; 可通过 --baseline-from 复用历史数据",
        ))

    # msprof component breakdown
    app_mb = msprof_components.get("APP", 0)
    hccl_mb = msprof_components.get("HCCL", 0)
    runtime_mb = msprof_components.get("RUNTIME", 0)
    slog_mb = msprof_components.get("SLOG", 0)
    other_msprof = sum(v for k, v in msprof_components.items()
                       if k not in ("APP", "HCCL", "RUNTIME", "SLOG") and v > 0)

    # Weight analysis: prefer vLLM log (actual measurement), fall back to
    # safetensors-precise, then config theory.  vLLM's DeviceMemoryProfiler
    # captures the real memory delta during weight loading, which accounts
    # for weight sharing, dtype conversion, and load-time filtering.
    weights_gb = vllm_info.get("weights_gb", 0)
    precise_gib = weight_precise.get("per_device_gib", 0) if weight_precise else 0
    theory_gib = weight_theory.get("per_device_weight_gib", 0)

    if weights_gb:
        weights_mb = weights_gb * 1024
        weight_source = "vLLM DeviceMemoryProfiler"
        weight_evidence = f"vllm_serve.log: Loading model weights took {weights_gb:.4f} GB"
    elif precise_gib:
        weights_mb = precise_gib * 1024
        weight_source = "safetensors 文件头精确计算"
        weight_evidence = f"weight_manifest.json: per_device = {precise_gib:.4f} GiB"
    else:
        weights_mb = 0
        weight_source = "不可用"
        weight_evidence = "无 vLLM 日志且无 safetensors 数据"

    weight_corr = []
    if precise_gib:
        weight_corr.append(f"safetensors 精确值: {precise_gib:.4f} GiB")
    if weights_gb:
        weight_corr.append(f"vLLM 日志: {weights_gb:.4f} GB")
    if precise_gib and weights_gb:
        diff_pct = abs(weights_gb - precise_gib) / precise_gib * 100 if precise_gib else 0
        weight_corr.append(f"safetensors vs vLLM 误差: {diff_pct:.1f}%")
    elif theory_gib and weights_gb:
        diff_pct = abs(weights_gb - theory_gib) / theory_gib * 100 if theory_gib else 0
        weight_corr.append(f"config 理论值: {theory_gib:.2f} GiB / 误差: {diff_pct:.1f}%")

    report.components.append(MemoryComponent(
        name="模型权重",
        value_mb=weights_mb,
        source=weight_source,
        evidence=weight_evidence,
        corroboration=" / ".join(weight_corr) if weight_corr else "",
    ))

    # KV cache
    kv_gib = vllm_info.get("kv_cache_available_gib", 0)
    kv_mb = kv_gib * 1024
    kv_tokens = vllm_info.get("kv_cache_tokens", 0)
    kv_evidence = f"vllm_serve.log: Available KV cache = {kv_gib:.2f} GiB"
    if kv_tokens:
        kv_evidence += f", {kv_tokens:,} tokens"

    report.components.append(MemoryComponent(
        name="KV Cache 预留",
        value_mb=kv_mb,
        source="vLLM 日志 'Available KV cache memory'",
        evidence=kv_evidence,
    ))

    scope_label = f" [{msprof_scope}]" if msprof_scope != "per-device" else ""

    # HCCL
    if hccl_mb > 0:
        report.components.append(MemoryComponent(
            name="HCCL 缓冲",
            value_mb=hccl_mb,
            source=f"msprof npu_module_mem Component=HCCL{scope_label}",
            evidence=f"npu_module_mem.csv: HCCL max = {hccl_mb:.2f} MB",
        ))

    # RUNTIME
    if runtime_mb > 0:
        report.components.append(MemoryComponent(
            name="CANN Runtime",
            value_mb=runtime_mb,
            source=f"msprof npu_module_mem Component=RUNTIME{scope_label}",
            evidence=f"npu_module_mem.csv: RUNTIME max = {runtime_mb:.2f} MB",
        ))

    # SLOG
    if slog_mb > 0:
        report.components.append(MemoryComponent(
            name="SLOG (系统日志)",
            value_mb=slog_mb,
            source=f"msprof npu_module_mem Component=SLOG{scope_label}",
            evidence=f"npu_module_mem.csv: SLOG max = {slog_mb:.2f} MB",
        ))

    # Other msprof components
    if other_msprof > 0.5:
        report.components.append(MemoryComponent(
            name="其他 msprof 组件",
            value_mb=other_msprof,
            source=f"msprof npu_module_mem (minor components){scope_label}",
            evidence=f"Sum of minor components = {other_msprof:.2f} MB",
        ))

    # ACL graph capture memory (from vLLM log)
    graph_gib = vllm_info.get("graph_capture_gib", 0)
    graph_mb = graph_gib * 1024
    if graph_mb > 0:
        graph_sizes = vllm_info.get("acl_graph_sizes_count", "?")
        report.components.append(MemoryComponent(
            name="ACL Graph 编译缓冲",
            value_mb=graph_mb,
            source="vLLM 日志 'Graph capturing took X GiB'",
            evidence=f"vllm_serve.log: graph capture = {graph_gib:.2f} GiB, {graph_sizes} sizes",
        ))

    # Activation estimate (delta between inference and ready states)
    infer_mb = infer.get("used_mb", used_mb)
    activation_mb = max(0, infer_mb - used_mb)
    if activation_mb > 0:
        report.components.append(MemoryComponent(
            name="激活峰值",
            value_mb=activation_mb,
            source="npu-smi delta (inference - ready)",
            evidence=f"after_infer={infer_mb} MB - after_ready={used_mb} MB = {activation_mb} MB",
        ))

    # Compute unattributed and try to sub-attribute it
    component_sum = sum(c.value_mb for c in report.components)
    unattributed = used_mb - component_sum

    has_msprof = app_mb > 0
    _handle_residual(report, unattributed, has_msprof)

    # Cross-validation
    report.cross_validation = {
        "npu_smi_used_mb": used_mb,
        "component_sum_mb": round(component_sum, 1),
        "unattributed_mb": round(unattributed, 1),
        "unattributed_pct": round(abs(unattributed) / used_mb * 100, 1) if used_mb else 0,
    }
    if app_mb > 0:
        report.cross_validation["msprof_app_mb"] = round(app_mb, 1)
        weight_kv_mb = weights_mb + kv_mb
        report.cross_validation["vllm_weight_plus_kv_mb"] = round(weight_kv_mb, 1)
        app_vs_wkv_diff = app_mb - weight_kv_mb
        report.cross_validation["app_minus_weight_kv_mb"] = round(app_vs_wkv_diff, 1)

    # Compute percentages
    for c in report.components:
        c.delta_pct = round(c.value_mb / used_mb * 100, 1) if used_mb else 0

    return report


def _handle_residual(
    report: DeviceReport,
    unattributed: float,
    has_msprof: bool,
) -> None:
    """Handle unattributed memory in the report.

    When msprof data is available, HCCL/RUNTIME/SLOG are already precise
    components — the residual should be small and is reported as-is.
    When msprof data is missing, the residual is marked as untraceable.
    No estimation or guessing is performed in either case.
    """
    if abs(unattributed) <= 1:
        return

    if has_msprof:
        report.components.append(MemoryComponent(
            name="  └ 未归因残差",
            value_mb=unattributed,
            source="残差: npu-smi 已用 - 所有已知组件加总",
            evidence=f"残差 {unattributed:.0f} MB, 可能来源: 算子 workspace、分配器对齐开销、碎片等",
        ))
    else:
        report.components.append(MemoryComponent(
            name="  └ 未归因 (缺少 msprof 数据)",
            value_mb=unattributed,
            source="不可追溯: 缺少 msprof npu_module_mem 数据，无法拆分为 HCCL/RUNTIME/SLOG",
            evidence=f"残差 {unattributed:.0f} MB, 需使用 msprof 采集后重新分析以获得精确拆分",
        ))


def format_report_text(
    reports: list[DeviceReport],
    manifest: dict,
    weight_precise: dict | None = None,
) -> str:
    """Format reports as human-readable text."""
    lines = []
    lines.append("=" * 70)
    lines.append("  Ascend 显存 Profiling 报告")
    lines.append("=" * 70)
    lines.append(f"模型: {manifest.get('model', '?')}")
    dp = manifest.get('dp', 1)
    tp_dp_str = f"TP={manifest.get('tp', '?')}"
    if dp and dp > 1:
        tp_dp_str += f", DP={dp}"
    tp_dp_str += f" | 设备: {manifest.get('devices', '?')}"
    spec_cfg = manifest.get('speculative_config', '')
    if spec_cfg:
        tp_dp_str += f"\n推测解码: {spec_cfg}"
    comp_cfg = manifest.get('compilation_config', '')
    if comp_cfg:
        tp_dp_str += f"\n编译配置: {comp_cfg}"
    quant = manifest.get('quantization', '')
    if quant:
        tp_dp_str += f"\n量化方式: {quant}"
    lines.append(tp_dp_str)
    mode = manifest.get("mode", "standalone")
    lines.append(f"采集模式: {'attach (挂载已有服务)' if mode == 'attach' else 'standalone (独立管理服务)'}")
    lines.append(f"msprof 采集: {'是' if manifest.get('msprof_enabled') else '否'}")
    if mode == "attach":
        baseline_src = manifest.get("baseline_source", "unavailable")
        if baseline_src == "unavailable":
            lines.append("基线数据: 不可用 (attach 模式，建议通过 --baseline-from 复用历史基线)")
        else:
            lines.append(f"基线数据: 复用自 {baseline_src}")
        svc_ref = manifest.get("serving_state_ref", "")
        if svc_ref:
            lines.append(f"服务状态: {svc_ref}")
    lines.append("")

    for r in reports:
        lines.append(f"[Device {r.device_id}] 总 HBM: {r.total_hbm_mb / 1024:.2f} GiB | "
                     f"已用: {r.used_hbm_mb / 1024:.2f} GiB")
        lines.append("")
        lines.append(f"{'组件':<28} | {'占用 (MB)':>10} | {'占用 (GiB)':>10} | {'占比':>6} | 主数据源")
        lines.append("-" * 110)

        for c in r.components:
            lines.append(
                f"{c.name:<28} | {c.value_mb:>10.1f} | {c.value_mb/1024:>10.3f} | "
                f"{c.delta_pct:>5.1f}% | {c.source}"
            )
        lines.append("")

        lines.append("[交叉验证]")
        cv = r.cross_validation
        lines.append(f"  npu-smi 已用:          {cv.get('npu_smi_used_mb', 0):,.0f} MB")
        lines.append(f"  组件加总:              {cv.get('component_sum_mb', 0):,.1f} MB")
        lines.append(f"  未归因:                {cv.get('unattributed_mb', 0):,.1f} MB "
                     f"({cv.get('unattributed_pct', 0):.1f}%)")
        if "msprof_app_mb" in cv:
            lines.append(f"  msprof APP:            {cv['msprof_app_mb']:,.1f} MB")
            lines.append(f"  vLLM 权重+KV:          {cv.get('vllm_weight_plus_kv_mb', 0):,.1f} MB")
            lines.append(f"  APP - (权重+KV):       {cv.get('app_minus_weight_kv_mb', 0):,.1f} MB "
                         "(含激活/内部缓冲)")
        lines.append("")

        lines.append("[证据链]")
        for c in r.components:
            lines.append(f"  {c.name}:")
            lines.append(f"    来源: {c.source}")
            lines.append(f"    证据: {c.evidence}")
            if c.corroboration:
                lines.append(f"    印证: {c.corroboration}")
        lines.append("")
        lines.append("=" * 70)

    if weight_precise and weight_precise.get("categories"):
        vllm_gb = 0
        for r in reports:
            for c in r.components:
                if "权重" in c.name:
                    vllm_gb = c.value_mb / 1024
                    break
            if vllm_gb:
                break
        lines.append("")
        lines.extend(format_weight_breakdown(weight_precise, vllm_gb))
        lines.append("=" * 70)

    return "\n".join(lines)


def find_msprof_csv(run_dir: Path, pattern: str) -> Path | None:
    """Find a msprof CSV file matching a pattern (returns first match)."""
    csv_dir = run_dir / "msprof_csvs"
    if not csv_dir.exists():
        return None
    for f in csv_dir.iterdir():
        if pattern in f.name and f.suffix == ".csv":
            return f
    return None


def find_all_msprof_csvs(run_dir: Path, pattern: str) -> list[Path]:
    """Find all msprof CSV files matching a pattern."""
    csv_dir = run_dir / "msprof_csvs"
    if not csv_dir.exists():
        return []
    return sorted(f for f in csv_dir.iterdir() if pattern in f.name and f.suffix == ".csv")


def main() -> None:
    p = argparse.ArgumentParser(description="Analyze Ascend memory profiling data")
    p.add_argument("run_dir", help="Path to collection run directory")
    p.add_argument("--format", choices=["json", "text"], default="json",
                   help="Output format on stdout (default: json)")
    args = p.parse_args()

    run_dir = Path(args.run_dir)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: {manifest_path} not found", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text())

    # Parse vLLM logs
    log_path = run_dir / "vllm_serve.log"
    vllm_info = parse_vllm_logs(log_path.read_text()) if log_path.exists() else {}

    # Parse msprof data — build per-device component dicts.
    # The manifest's __prof_device_map__ maps CSV filenames to device IDs,
    # allowing per-device attribution when DP > 1 (multiple PROF directories).
    prof_device_map: dict[str, list[int]] = {}
    msprof_csvs_meta = manifest.get("msprof_csvs", {})
    if isinstance(msprof_csvs_meta, dict):
        prof_device_map = msprof_csvs_meta.get("__prof_device_map__", {})

    per_device_msprof: dict[int, dict[str, float]] = {}
    global_msprof: dict[str, float] = {}

    for csv_path in find_all_msprof_csvs(run_dir, "npu_module_mem"):
        partial = parse_npu_module_mem_csv(csv_path)
        devs = prof_device_map.get(csv_path.name, [])

        if devs:
            for dev_id in devs:
                if dev_id not in per_device_msprof:
                    per_device_msprof[dev_id] = {}
                for comp, val in partial.items():
                    if val > per_device_msprof[dev_id].get(comp, 0):
                        per_device_msprof[dev_id][comp] = val
        # Always merge into global for fallback / cross-validation
        for comp, val in partial.items():
            if val > global_msprof.get(comp, 0):
                global_msprof[comp] = val

    # Weight analysis: prefer safetensors manifest, fallback to config estimate
    tp = manifest.get("tp", 1)
    dp = manifest.get("dp", 1)

    weight_manifest_path = run_dir / "weight_manifest.json"
    weight_manifest = {}
    if weight_manifest_path.exists():
        weight_manifest = json.loads(weight_manifest_path.read_text())

    mc = manifest.get("model_config", {})
    has_experts = bool(
        mc.get("num_experts", 0)
        or mc.get("text_config", {}).get("num_experts", 0)
    )
    ep_flag = manifest.get("enable_expert_parallel")
    enable_ep = ep_flag if ep_flag is not None else has_experts
    weight_precise = compute_weight_from_manifest(weight_manifest, tp, dp, enable_ep) if weight_manifest else {}

    model_config = manifest.get("model_config", {})
    if not model_config:
        cfg_path = run_dir / "model_config.json"
        if cfg_path.exists():
            model_config = json.loads(cfg_path.read_text())
    weight_theory = estimate_weight_size(model_config, tp, dp) if model_config else {}

    # Parse npu-smi data
    baseline_hbm = parse_npu_smi_hbm(manifest.get("baseline_hbm", {}))
    after_ready_hbm = parse_npu_smi_hbm(manifest.get("after_ready_hbm", {}))
    after_infer_hbm = parse_npu_smi_hbm(manifest.get("after_infer_hbm", {}))

    # Generate per-device reports
    total_devices = tp * dp
    devices = sorted(set(list(baseline_hbm.keys()) + list(after_ready_hbm.keys())))
    if not devices:
        devices = list(range(total_devices))

    reports = []
    for dev_id in devices:
        if isinstance(dev_id, int) and dev_id >= total_devices:
            continue
        device_msprof = per_device_msprof.get(dev_id, global_msprof)
        report = generate_device_report(
            dev_id=dev_id,
            baseline_hbm=baseline_hbm,
            after_ready_hbm=after_ready_hbm,
            after_infer_hbm=after_infer_hbm,
            msprof_components=device_msprof,
            vllm_info=vllm_info,
            weight_theory=weight_theory,
            weight_precise=weight_precise,
            tp=tp,
            msprof_scope="per-device" if dev_id in per_device_msprof else "process-level",
        )
        reports.append(report)

    # Format and save report
    text_report = format_report_text(reports, manifest, weight_precise)
    (run_dir / "report.txt").write_text(text_report)

    json_report = {
        "manifest": manifest,
        "vllm_info": vllm_info,
        "msprof_components_global": {k: round(v, 2) for k, v in global_msprof.items()},
        "msprof_per_device": {str(d): {k: round(v, 2) for k, v in comps.items()} for d, comps in per_device_msprof.items()},
        "weight_theory": weight_theory,
        "weight_precise": weight_precise,
        "devices": [asdict(r) for r in reports],
    }
    json_str = json.dumps(json_report, indent=2, ensure_ascii=False)
    (run_dir / "report.json").write_text(json_str)

    if args.format == "json":
        print(json_str)
    else:
        print(text_report)

    print(f"\nReport saved to: {run_dir / 'report.txt'}", file=sys.stderr)
    print(f"JSON report:     {run_dir / 'report.json'}", file=sys.stderr)


if __name__ == "__main__":
    main()
