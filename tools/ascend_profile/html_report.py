#!/usr/bin/env python3
"""HTML report renderer for an Ascend profiling analysis root.

Reads the CSV / JSON artefacts already produced by `tools/ascend_profile`
and emits a single-file, zero-dependency HTML report with three SPA-style
views (no long scroll):

  - L1 (overview): cross-rank step Gantt with quick/slow cards, DP/EP
    load summary (EP peak-to-mean via GroupedMatmul wall), companion-run
    detection. Clicking a step opens L2.
  - L2 (per-step): cross-rank compare for the clicked step, phase split
    (main / speculative / tail / bubble — all using union active time so
    concurrent AIC+AIV don't double count), kernel rollup sorted by share
    of step-active, and a layer order table. Clicking a layer opens L3.
  - L3 (per-layer / per-tail-block): execution-order operator list with
    stream id and on-click operator cards showing raw kernel_details fields,
    pyfunc-style IR signature, CANN pipeline ratios, plus self/layer/step
    cumulative share metrics.

Entry points:
  - CLI:    python -m tools.ascend_profile.html_report <analysis_root> <out.html>
  - Python: build_html_report(analysis_root, output_path)

Internal hash IDs (segment_id / step_class_id / layer_class_id / block_class_id)
are not surfaced in the rendered UI; they remain only as DOM anchor targets.
"""

from __future__ import annotations

import bisect
import csv
import html
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

csv.field_size_limit(10 * 1024 * 1024)

PALETTE = {
    "bg": "#0d1117",
    "bg_card": "#161b22",
    "bg_card_alt": "#1c232c",
    "border": "#30363d",
    "text": "#e6edf3",
    "muted": "#8b949e",
    "accent": "#58a6ff",
    "warn": "#f0883e",
    "danger": "#f85149",
    "success": "#3fb950",
}

FAMILY_COLOR = {
    "attention_moe_workload": "#58a6ff",
    "moe_or_dummy_workload": "#d2a8ff",
    "attention_dense_workload": "#3fb950",
    "ffn_or_dummy_workload": "#f0883e",
    "communication_only": "#f85149",
    "mixed_workload": "#bc8cff",
}

BLOCK_COLOR = {
    "attention": "#58a6ff",
    "moe": "#d2a8ff",
    "ffn": "#f0883e",
    "aicpu": "#8b949e",
    "other": "#484f58",
}

OP_TYPE_COLOR = {
    "aic": "#79c0ff",
    "aiv": "#d2a8ff",
    "mix_cv": "#ffa657",
    "mix_comm_aiv": "#f0883e",
    "communication": "#f85149",
    "aicpu": "#a371f7",
    "dsa": "#7ee787",
    "unknown": "#8b949e",
}

BOUND_FAMILY_COLOR = {
    "cube": "#79c0ff",
    "vector": "#d2a8ff",
    "aic_mte": "#58a6ff",
    "aiv_mte": "#bc8cff",
    "scalar": "#ffa657",
    "mixed": "#f0883e",
    "communication": "#f85149",
    "comm_aiv_mix": "#ff7b72",
    "aicpu": "#a371f7",
    "dsa": "#7ee787",
    "unknown": "#8b949e",
}

# kernel_details 字段文档：hover tooltip 显示
FIELD_DOC = {
    "self_layer_pct": "本算子**单次**执行耗时 / 当前 layer 在本 rank 上所有 device 事件 (AIV/AIC/mix_cv/mix_comm_aiv/communication/aicpu) 的 active union 时长（去 redundant，跨流取并集）。问的是：'这一次调用占当前 layer 多少？'",
    "klayer_pct": "本类 kernel（按 short_op_name 聚合：aclnn 算子保留到第一个 `_` 前的 API 名，如 `aclnnCausalConv1d_*` → `aclnnCausalConv1d`；HCCL 算子保留到第一个 `__` 前，如 `hcom_allReduce__503_150_1` → `hcom_allReduce`；其余保持原名）在**当前 layer 内**所有调用的 union 耗时 / 当前 layer 同口径的 active union 时长。括号里是该类 kernel 在本 layer 内的调用次数。",
    "kstep_pct": "本类 kernel 在**当前 step 内（本 rank 上）**所有调用的 union 耗时 / 当前 step 在本 rank 上所有 device 事件 (AIV/AIC/mix_cv/mix_comm_aiv/communication/aicpu) 的 active union 时长（去 redundant，跨流取并集，不含 bubble）。括号里是该类 kernel 在本 step 内的总调用次数。问的是：'这类算子在整个 step 中（不含 bubble）占多少？' 这是评估 kernel 在一次 forward pass 中重要性的核心指标。",
    "ep_peak_to_mean": "EP 峰均比 = max(rank GMM 总耗时) / mean(rank GMM 总耗时)，>= 1。GroupedMatmul 是 MoE 各 expert dispatch 后的核心计算 kernel，每 rank 上的 GMM 总耗时直接反映该 rank 被分到的 token 量。经验阈值：>1.10 视为 EP 不均；>1.30 严重热点。",
    "ep_per_rank_gmm": "该 rank 上所有 GroupedMatmul/GroupedMatmulV5 算子的 wall-time 总和。",
    "speculative_layer": "投机解码 (speculative decoding) 的辅助层。layer_role = 'speculative' / 'spec' / 'spec_layer' 时被归入此类。投机层用 draft model 提前并行预测 N 个 token，主模型用一次大 forward 验证哪些预测正确。",
    "duration_us": "算子在 device 上的实际执行时间 (μs)。来自 kernel_details.csv 的 Duration 列。",
    "wait_us": "算子从就绪到真正开始执行之间的等待时间 (μs)。常见来源：等 stream sync、等 HCCL Notify Wait、等数据依赖；wait 过大通常说明 host bound 或上游 collective 慢。",
    "stream_id": "Device 侧执行流 ID。同一 rank 上不同 stream 可以并行执行；'N/A' 通常代表 HCCL 默认通信流。",
    "aicore_time": "AIC（AI Core，Cube）流水线累计时间。",
    "aiv_time": "AIV（AI Vector）流水线累计时间。",
    "aic_mac_time": "AIC MAC pipe（矩阵乘单元）累计耗时。计算 bound 的关键 stage；mac 高说明真正的 GEMM compute 在跑。",
    "aic_fixpipe_time": "AIC FixPipe（结果搬出 + 后处理 + cast）累计耗时。",
    "aic_mte1_time": "AIC MTE1：L1 → BT/SMEM 等核内搬运。",
    "aic_mte2_time": "AIC MTE2：外部存储 → L1（DDR/L2 → L1）。GroupedMatmul / MatMul 等大算子常 mte2 bound，说明 DDR 带宽是瓶颈。",
    "aic_scalar_time": "AIC scalar pipe：取指、地址计算、控制流。scalar 偏高通常说明 kernel 中 control flow 太重 或 block_dim 切分不合理。",
    "aiv_vec_time": "AIV Vector pipe：逐元素向量运算（add/mul/exp 等）。",
    "aiv_mte2_time": "AIV MTE2：外部存储 → UB（Unified Buffer）。",
    "aiv_mte3_time": "AIV MTE3：UB → 外部存储。",
    "aiv_scalar_time": "AIV scalar pipe（同 AIC scalar，作用在 AIV 上）。",
    "shape_signature": "算子输入/输出 tensor shape 的归一化签名，shape-strict 分组用。",
    "op_type": "算子大类：aic=纯 AIC（Cube 核）；aiv=纯 AIV（Vector 核）；mix_cv=AIC+AIV 混合（FlashAttention、GroupedMatmul 等）；mix_comm_aiv=通信+AIV 融合（dispatch/combine）；communication=纯 HCCL；aicpu=芯片上的标量 AI CPU 核（device 侧，不是 host CPU）。",
    "bound_stage": "该算子流水线中累计耗时最长的单 stage，通常就是该 kernel 的性能瓶颈。",
    "bound_family": "bound_stage 的粗粒度归类：cube / vector / aic_mte / aiv_mte / scalar / mixed / communication / ...",
    "comm_share": "该范围内 HCCL 算子（+ mix_comm_aiv）累计耗时 / 该范围 wall_ms。衡量通信开销占比。",
    "block_kind": "Layer 内部 block 的功能分类：attention / ffn / moe（aicpu / other 会被合并到相邻 block）。",
    "companion_layer": "该 layer 缺少 attention block，通常是陪跑 / dummy 数据 / warmup 等结构。",
}

# AIC / AIV stage 分组：用于在算子卡里按归属展示
AIC_STAGES = ["aic_mac_time", "aic_fixpipe_time", "aic_mte1_time", "aic_mte2_time", "aic_scalar_time"]
AIV_STAGES = ["aiv_vec_time", "aiv_mte2_time", "aiv_mte3_time", "aiv_scalar_time"]

# bound_stage → 决策依据字段映射
STAGE_FAMILY = {
    "aic_mac_time": "cube",
    "aic_fixpipe_time": "aic_mte",
    "aic_mte1_time": "aic_mte",
    "aic_mte2_time": "aic_mte",
    "aic_scalar_time": "scalar",
    "aiv_vec_time": "vector",
    "aiv_mte2_time": "aiv_mte",
    "aiv_mte3_time": "aiv_mte",
    "aiv_scalar_time": "scalar",
}

# pipeline stage → 对应 ratio 列名（原始 kernel_details.csv 字段名）
STAGE_RATIO_FIELD = {
    "aic_mac_time": "aic_mac_ratio",
    "aic_scalar_time": "aic_scalar_ratio",
    "aic_mte1_time": "aic_mte1_ratio",
    "aic_mte2_time": "aic_mte2_ratio",
    "aic_fixpipe_time": "aic_fixpipe_ratio",
    "aiv_vec_time": "aiv_vec_ratio",
    "aiv_scalar_time": "aiv_scalar_ratio",
    "aiv_mte2_time": "aiv_mte2_ratio",
    "aiv_mte3_time": "aiv_mte3_ratio",
}

# 原始 kernel_details.csv 完整 schema (46 列)，用于算子卡 tier 3 的 raw dump
RAW_KD_FIELDS = [
    "Device_id", "Model ID", "Task ID", "Stream ID", "Name", "Type", "OP State",
    "Accelerator Core", "Start Time(us)", "Duration(us)", "Wait Time(us)",
    "Block Dim", "Mix Block Dim", "HF32 Eligible",
    "Input Shapes", "Input Data Types", "Input Formats",
    "Output Shapes", "Output Data Types", "Output Formats",
    "Context ID",
    "aicore_time(us)", "aic_total_cycles",
    "aic_mac_time(us)", "aic_mac_ratio",
    "aic_scalar_time(us)", "aic_scalar_ratio",
    "aic_mte1_time(us)", "aic_mte1_ratio",
    "aic_mte2_time(us)", "aic_mte2_ratio",
    "aic_fixpipe_time(us)", "aic_fixpipe_ratio",
    "aic_icache_miss_rate",
    "aiv_time(us)", "aiv_total_cycles",
    "aiv_vec_time(us)", "aiv_vec_ratio",
    "aiv_scalar_time(us)", "aiv_scalar_ratio",
    "aiv_mte2_time(us)", "aiv_mte2_ratio",
    "aiv_mte3_time(us)", "aiv_mte3_ratio",
    "aiv_icache_miss_rate",
    "cube_utilization(%)",
]

# Ratio / utilization 字段说明（用于 hover tooltip）
RATIO_FIELD_DOC = {
    "aic_mac_ratio": "AIC MAC pipe 利用率 = aic_mac_time / aicore_time。接近 1 说明该 kernel 真在做 cube 计算；偏低则 cube 没吃满。",
    "aic_scalar_ratio": "AIC scalar pipe 利用率。偏高（>0.5）通常意味着 kernel 中控制流 / 索引计算过重 → 优化方向：合并 scalar、对齐 block_dim。",
    "aic_mte1_ratio": "AIC MTE1 利用率。L1↔BT 内部搬运占比。",
    "aic_mte2_ratio": "AIC MTE2 利用率。外存→L1 搬运占比；偏高（>0.6）说明 DDR/L2 带宽瓶颈。",
    "aic_fixpipe_ratio": "AIC FixPipe 利用率。结果搬出 + 后处理占比；矩阵乘类算子偏高常见。",
    "aiv_vec_ratio": "AIV Vector pipe 利用率。算子真正做向量计算的占比。",
    "aiv_scalar_ratio": "AIV scalar 利用率。同 AIC scalar，作用在 AIV 上。",
    "aiv_mte2_ratio": "AIV MTE2 利用率。外存→UB 搬运占比；偏高说明算子读外存压力大。",
    "aiv_mte3_ratio": "AIV MTE3 利用率。UB→外存 搬运占比；偏高说明算子写外存压力大。",
    "aic_icache_miss_rate": "AIC instruction cache miss 率。偏高说明 kernel 指令体过大 → block_dim 切得太细 / 重复编译。",
    "aiv_icache_miss_rate": "AIV instruction cache miss 率。同上。",
    "cube_utilization(%)": "Cube 单元的整体占比（百分制）。GEMM 类算子衡量真实计算密度。",
    "Block Dim": "AIC block dimension（拆 N 维度的并发数）。",
    "Mix Block Dim": "AIV block dimension（mix 算子时使用）。",
    "HF32 Eligible": "该算子能否使用 HF32 精度。",
    "Context ID": "执行上下文 ID（runtime 调度用）。",
    "Input Shapes": "输入 tensor 形状列表。",
    "Input Data Types": "输入 tensor dtype 列表。",
    "Input Formats": "输入 tensor format（ND / NCHW / FRACTAL_NZ ...）。",
    "Output Shapes": "输出 tensor 形状。",
    "Output Data Types": "输出 tensor dtype。",
    "Output Formats": "输出 tensor format。",
}
FIELD_DOC.update(RATIO_FIELD_DOC)


def short_op_name(name: str) -> str:
    """Normalize kernel name for grouping. Strategy: keep the original name.

    Only strip auto-generated suffixes that prevent aggregation:
      - aclgraph naming (`aclnn<API>_<OpsImpl>_<KernelName>`) → keep `aclnn<API>`
        e.g. `aclnnCausalConv1d_CausalConv1d_CausalConv1d` → `aclnnCausalConv1d`
              `aclnnInplaceFillScalar_FillAiCore_Fill` → `aclnnInplaceFillScalar`
      - HCCL sequence id (`hcom_<op>__<seq>_<group>_<idx>`) → keep `hcom_<op>`
        e.g. `hcom_allReduce__503_150_1` → `hcom_allReduce`
    Everything else (Triton-style kernels, bare op names) → original.
    """
    if not name:
        return ""
    if name.startswith("aclnn"):
        return name.split("_", 1)[0]
    if name.startswith("hcom_"):
        return name.split("__", 1)[0] if "__" in name else name
    return name


def short_rank_label(rank_id: str) -> str:
    parts = {}
    for tok in rank_id.split("_"):
        m = re.match(r"([a-z]+)(\d+)$", tok)
        if m:
            parts[m.group(1)] = int(m.group(2))
    bits = []
    if "dp" in parts:
        bits.append(f"dp{parts['dp']}")
    if "tp" in parts:
        bits.append(f"tp{parts['tp']}")
    if "pp" in parts and parts["pp"] != 0:
        bits.append(f"pp{parts['pp']}")
    if "ep" in parts:
        bits.append(f"ep{parts['ep']}")
    return "·".join(bits) if bits else rank_id


def family_label(family: str, layer_count: int | None = None) -> str:
    base = {
        "attention_moe_workload": "Attention + MoE",
        "moe_or_dummy_workload": "MoE-only / Dummy",
        "attention_dense_workload": "Attention + Dense",
        "ffn_or_dummy_workload": "FFN-only / Dummy",
        "communication_only": "Communication-only",
        "mixed_workload": "Mixed",
    }.get(family, family.replace("_", " ").title())
    if layer_count:
        return f"{base} · {layer_count}L"
    return base


def fmt_ms(v, prec=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{prec}f}"
    except Exception:
        return str(v)


# -----------------------------
# v7 analysis helpers
# -----------------------------

def compute_ep_balance(b) -> dict:
    """Compute EP load balance via GroupedMatmul wall-time per rank.

    Returns {by_rank, mean_us, peak_us, min_us, peak_to_mean, spread}.
    peak_to_mean (>=1) is the standard "EP imbalance" indicator — values close to 1
    mean ranks are balanced; >= 1.10 means at least one rank does noticeably more
    GMM work than the average (an EP hotspot rank).
    """
    by_rank: dict[str, float] = defaultdict(float)
    for e in b.events:
        if getattr(e, "redundant", False):
            continue
        nm = (e.name or "")
        if "GroupedMatmul" in nm:
            by_rank[e.rank_id] += e.duration_us
    if not by_rank:
        return {"by_rank": {}, "mean_us": 0.0, "peak_us": 0.0, "min_us": 0.0,
                "peak_to_mean": 1.0, "spread": 0.0, "available": False}
    vals = list(by_rank.values())
    mean = sum(vals) / len(vals)
    peak = max(vals)
    lo = min(vals)
    return {
        "by_rank": dict(by_rank),
        "mean_us": mean,
        "peak_us": peak,
        "min_us": lo,
        "peak_to_mean": (peak / mean) if mean > 0 else 1.0,
        "spread": ((peak - lo) / mean) if mean > 0 else 0.0,
        "available": True,
    }


def assess_companion_run(b) -> dict:
    """Identify step-indices where some ranks run real data while others ran dummy.

    Returns {companion_step_indices, n_companion, n_total_aligned, rank_family_counts,
             companion_rank_pairs}.
    """
    step_by_rank: dict[str, list] = defaultdict(list)
    for s in b.step_summary:
        step_by_rank[s["rank_id"]].append(s)
    for rid in step_by_rank:
        step_by_rank[rid].sort(key=lambda x: safe_float(x["start_us"]))
    if not step_by_rank:
        return {"companion_step_indices": [], "n_companion": 0, "n_total_aligned": 0,
                "rank_family_counts": {}, "companion_rank_pairs": []}
    rank_ids = sorted(step_by_rank.keys())
    n_steps = min(len(step_by_rank[r]) for r in rank_ids)
    real_set = {"attention_moe_workload", "attention_dense_workload"}
    dummy_set = {"moe_or_dummy_workload", "ffn_or_dummy_workload"}
    companion_indices = []
    pair_counts: Counter = Counter()
    for i in range(n_steps):
        real_ranks = []
        dummy_ranks = []
        for r in rank_ids:
            fam = step_by_rank[r][i].get("step_family", "")
            if fam in real_set:
                real_ranks.append(r)
            elif fam in dummy_set:
                dummy_ranks.append(r)
        if real_ranks and dummy_ranks:
            companion_indices.append(i)
            pair_counts[(tuple(real_ranks), tuple(dummy_ranks))] += 1
    rank_family_counts: dict[str, dict] = {}
    for r in rank_ids:
        rank_family_counts[r] = Counter(
            s.get("step_family", "") for s in step_by_rank[r]
        )
    return {
        "companion_step_indices": companion_indices,
        "n_companion": len(companion_indices),
        "n_total_aligned": n_steps,
        "rank_family_counts": {r: dict(c) for r, c in rank_family_counts.items()},
        "companion_rank_pairs": [
            {"real_ranks": list(rr), "dummy_ranks": list(dd), "count": int(c)}
            for (rr, dd), c in pair_counts.most_common(8)
        ],
    }


def detect_attention_subtype(b, row_start: int, row_end: int, rank_id: str) -> str:
    """Inspect kernels within [row_start, row_end) of `rank_id` to identify attention sub-type.

    Returns one of: 'mla' (DeepSeek-style sparse / compressed-KV), 'fa' (FlashAttention),
    'gqa' (FIA with grouped-query — assumed default for FIA users), 'attn' (unknown).
    """
    events = events_in_row_range(b.events, row_start, row_end, rank_id)
    name_set = {short_op_name(e.name) for e in events}
    name_lower = {n.lower() for n in name_set}
    mla_markers = ("matmulcompressedkv", "absorbmatmul", "mlaprolog",
                   "sparseattnsharedkv", "compressor")
    if any(any(m in n for m in mla_markers) for n in name_lower):
        return "mla"
    if any("flashattention" in n for n in name_lower):
        return "fa"
    if any("fusedinferattention" in n or n.startswith("fia") for n in name_lower):
        return "gqa"  # vLLM-Ascend uses FIA for GQA/MHA; sub-precision needs shape parse
    return "attn"


def derive_layer_composition(b, ls: dict) -> str:
    """Derive layer composition from block_segments, e.g. 'gqa+moe', 'mla+ffn', 'moe'.

    Falls back to '—' when no blocks are recorded under this layer.
    """
    rid = ls["rank_id"]
    r_start = int(safe_float(ls["row_start"]))
    r_end = int(safe_float(ls["row_end"]))
    blocks = [
        bs for bs in b.block_segments
        if bs["rank_id"] == rid
        and int(safe_float(bs["row_start"])) >= r_start
        and int(safe_float(bs["row_end"])) <= r_end
    ]
    blocks.sort(key=lambda x: int(safe_float(x["row_start"])))
    parts = []
    for bs in blocks:
        kind = (bs.get("block_kind") or "").lower()
        if kind == "attention":
            sub = detect_attention_subtype(
                b,
                int(safe_float(bs["row_start"])),
                int(safe_float(bs["row_end"])),
                rid,
            )
            parts.append(sub)
        elif kind == "moe":
            parts.append("moe")
        elif kind in ("ffn", "mlp", "dense"):
            parts.append("ffn")
        elif kind:
            parts.append(kind)
    return "+".join(parts) if parts else "—"


def guess_model_structure(b, step_row: dict) -> str | None:
    """Honest structural fingerprint — *not* a model name guess.

    Returns 'NL · <attn_sub>+<ffn_or_moe>' if attention subtype is detectable, else None.
    The naming has been deliberately downgraded from "model id" → "structure" because
    structurally-different checkpoints (e.g. DeepSeek-V2-Lite 27L MLA vs Qwen-3.5 MoE 27L FIA)
    used to collide on (layer_count, has_attn, has_moe).
    """
    layer_count = int(safe_float(step_row.get("main_layer_count")))
    has_attn = str(step_row.get("has_attention", "")).lower() == "true"
    has_moe = str(step_row.get("has_moe", "")).lower() == "true"
    if not has_attn and not has_moe:
        return None
    rid = step_row["rank_id"]
    # use the step's row range as the inspection window
    seg_id = step_row.get("segment_id")
    seg = next((s for s in b.step_segments if s["segment_id"] == seg_id), None)
    if seg is None:
        return f"{layer_count}L · ?+{'moe' if has_moe else ('ffn' if has_attn else '?')}"
    attn_sub = detect_attention_subtype(
        b,
        int(safe_float(seg["row_start"])),
        int(safe_float(seg["row_end"])),
        rid,
    ) if has_attn else None
    rhs = "moe" if has_moe else ("ffn" if has_attn else "?")
    if has_attn:
        return f"{layer_count}L · {attn_sub}+{rhs}"
    return f"{layer_count}L · {rhs}"


# kept as alias for older callers
guess_model_id = guess_model_structure


# Everything in normalized_event_index.csv (i.e. originally from kernel_details.csv +
# HCCL traces) runs on device — AIV/AIC/mix_cv/mix_comm_aiv/communication/aicpu.
# `aicpu` is the chip's scalar AI CPU core (not host CPU) and must be counted as
# device-active. Only host-side events (Python / dispatcher / launch overhead,
# which are NOT in our event index) would be excluded — we never get them here.


def union_duration_us(events) -> float:
    """Merge-intervals union of event time spans (microseconds).

    Use this instead of `sum(e.duration_us)` whenever you want a real
    "active wall" for a section — summing double-counts events that happen
    concurrently on different streams (e.g. AIC stream + AIV stream both
    busy at the same wall-clock μs).

    Counts all non-redundant events. redundant flag is set by `dedup_comm_aiv`
    to mark dual-stream copies of an HCCL event so they're not double-counted.
    """
    intervals = []
    for e in events:
        if getattr(e, "redundant", False):
            continue
        if e.end_us <= e.start_us:
            continue
        intervals.append((e.start_us, e.end_us))
    if not intervals:
        return 0.0
    intervals.sort()
    total = 0.0
    cur_s, cur_e = intervals[0]
    for s, end in intervals[1:]:
        if s <= cur_e:
            if end > cur_e:
                cur_e = end
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, end
    total += cur_e - cur_s
    return total


def union_duration_us_by_name(events) -> dict:
    """Group events by short_op_name, return union duration per group (device-wide)."""
    by_name = defaultdict(list)
    for e in events:
        if getattr(e, "redundant", False):
            continue
        by_name[short_op_name(e.name)].append(e)
    return {k: union_duration_us(v) for k, v in by_name.items()}


def split_main_speculative_tail(b, step_seg: dict, rank_id: str) -> dict:
    """For a given step segment, split events into: head / main / speculative / tail / bubble buckets.

    Returns durations + event lists per bucket (events already rank-filtered).
    Speculative = events inside layers tagged as speculative.
    """
    step_row_start = step_seg["row_start"]
    step_row_end = step_seg["row_end"]
    step_events = events_in_row_range(b.events, step_row_start, step_row_end, rank_id)

    seg_id = step_seg["segment_id"]
    anatomy = next((a for a in b.step_anatomy if a["segment_id"] == seg_id), None)
    head_row_start = int(safe_float((anatomy or {}).get("head_row_start") or step_row_start))
    head_row_end   = int(safe_float((anatomy or {}).get("head_row_end") or step_row_start))
    main_row_start = int(safe_float((anatomy or {}).get("main_row_start") or step_row_start))
    main_row_end   = int(safe_float((anatomy or {}).get("main_row_end") or step_row_end))
    tail_row_start = int(safe_float((anatomy or {}).get("tail_row_start") or step_row_end))
    tail_row_end   = int(safe_float((anatomy or {}).get("tail_row_end") or step_row_end))

    # speculative layers within this step
    spec_layers = [
        ls for ls in b.layer_segments
        if ls["rank_id"] == rank_id
        and ls["row_start"] >= step_row_start
        and ls["row_end"] <= step_row_end
        and ls.get("layer_role") in ("speculative", "spec", "spec_layer")
    ]
    spec_rows = set()
    for ls in spec_layers:
        for rr in range(int(ls["row_start"]), int(ls["row_end"])):
            spec_rows.add(rr)

    def in_range(e, rs, re_):
        return rs <= e.row_idx < re_

    head_evts, main_evts, spec_evts, tail_evts = [], [], [], []
    for e in step_events:
        if e.row_idx in spec_rows:
            spec_evts.append(e)
        elif in_range(e, head_row_start, head_row_end):
            head_evts.append(e)
        elif in_range(e, tail_row_start, tail_row_end):
            tail_evts.append(e)
        elif in_range(e, main_row_start, main_row_end):
            main_evts.append(e)

    head_us = union_duration_us(head_evts)
    main_us = union_duration_us(main_evts)
    spec_us = union_duration_us(spec_evts)
    tail_us = union_duration_us(tail_evts)
    step_busy_us = union_duration_us(step_events)

    return {
        "step_events": step_events,
        "head_events": head_evts,
        "main_events": main_evts,
        "spec_events": spec_evts,
        "tail_events": tail_evts,
        "spec_layer_count": len(spec_layers),
        "head_us":  head_us,
        "main_us":  main_us,
        "spec_us":  spec_us,
        "tail_us":  tail_us,
        "step_busy_us": step_busy_us,
        "head_bubble_ms": safe_float((anatomy or {}).get("head_bubble_ms", 0)),
        "main_bubble_ms": safe_float((anatomy or {}).get("main_bubble_ms", 0)),
        "tail_bubble_ms": safe_float((anatomy or {}).get("tail_bubble_ms", 0)),
        "step_wall_ms": safe_float(step_seg.get("wall_ms", 0)) or (safe_float(step_seg["end_us"]) - safe_float(step_seg["start_us"])) / 1000.0,
    }


def kernel_rollup_by_bound(events: list) -> list:
    """Roll up events by (op_type, kernel name family) with bound-stage majority.

    Returns sorted list (desc by duration_us) of:
        {kernel, op_type, count, duration_us, bound_family, dominant_stage}
    """
    by_key: dict = defaultdict(lambda: {
        "count": 0,
        "duration_us": 0.0,
        "wait_us": 0.0,
        "op_type": "",
        "stage_durations": defaultdict(float),
    })
    for e in events:
        if getattr(e, "redundant", False):
            continue
        key = (short_op_name(e.name), e.op_type)
        rec = by_key[key]
        rec["count"] += 1
        rec["duration_us"] += e.duration_us
        rec["wait_us"] += getattr(e, "wait_us", 0)
        rec["op_type"] = e.op_type
        for stage_field, v in (e.pipeline or {}).items():
            rec["stage_durations"][stage_field] += safe_float(v)
    rows = []
    for (kernel, op_type), rec in by_key.items():
        bound = pick_bound_stage(rec["stage_durations"]) if rec["stage_durations"] else None
        family = STAGE_FAMILY.get(bound, "unknown") if bound else "unknown"
        rows.append({
            "kernel": kernel,
            "op_type": op_type,
            "count": rec["count"],
            "duration_us": rec["duration_us"],
            "wait_us": rec["wait_us"],
            "bound_stage": bound or "—",
            "bound_family": family,
        })
    rows.sort(key=lambda r: -r["duration_us"])
    return rows


def fmt_pct(v, prec=1):
    if v is None:
        return "—"
    try:
        return f"{float(v)*100:.{prec}f}%"
    except Exception:
        return str(v)


def safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def hue_shift(base_hex: str, shift: int) -> str:
    base_hex = base_hex.lstrip("#")
    r = int(base_hex[0:2], 16)
    g = int(base_hex[2:4], 16)
    b = int(base_hex[4:6], 16)
    if shift >= 0:
        f = shift / 100.0
        r = int(r + (255 - r) * f)
        g = int(g + (255 - g) * f)
        b = int(b + (255 - b) * f)
    else:
        f = 1.0 + shift / 100.0
        r = int(r * f)
        g = int(g * f)
        b = int(b * f)
    return f"#{r:02x}{g:02x}{b:02x}"


def class_color(family, step_class_id):
    base = FAMILY_COLOR.get(family, "#58a6ff")
    if not step_class_id:
        return base
    bucket = sum(ord(c) for c in step_class_id) % 5
    offset = [-18, -9, 0, 9, 18][bucket]
    return hue_shift(base, offset)


def load_csv(path: Path):
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def load_json(path: Path):
    if not path.exists():
        return None
    with path.open() as f:
        return json.load(f)


@dataclass
class Event:
    event_id: str
    rank_id: str
    source_id: str
    row_idx: int
    name: str
    task_type: str
    op_type: str
    accel_core: str
    stream_id: str
    start_us: float
    end_us: float
    duration_us: float
    wait_us: float
    pipeline: dict
    shape_signature: str
    op_roles: str
    op_categories: str
    redundant: bool = False  # 通信去重 flag
    raw_row: dict = field(default_factory=dict)  # full kernel_details.csv row (46 fields)


@dataclass
class Bundle:
    root: Path
    rank_summary: list = field(default_factory=list)
    step_summary: list = field(default_factory=list)
    step_anatomy: list = field(default_factory=list)
    step_class: list = field(default_factory=list)
    layer_class: list = field(default_factory=list)
    block_class: list = field(default_factory=list)
    operator_class: list = field(default_factory=list)
    hccl_class: list = field(default_factory=list)
    hccl_op: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    manifest: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    step_segments: list = field(default_factory=list)
    layer_segments: list = field(default_factory=list)
    block_segments: list = field(default_factory=list)


def _load_segments(path: Path, key: str) -> list:
    data = load_json(path)
    if data is None:
        return []
    if isinstance(data, dict) and key in data:
        return data[key]
    if isinstance(data, list):
        return data
    return []


def _load_events(path: Path) -> list:
    if not path.exists():
        return []
    events = []
    with path.open() as f:
        for row in csv.DictReader(f):
            try:
                pipe = json.loads(row.get("pipeline_us") or "{}")
            except Exception:
                pipe = {}
            events.append(Event(
                event_id=row["event_id"],
                rank_id=row["rank_id"],
                source_id=row.get("source_id", ""),
                row_idx=int(row.get("row_idx") or 0),
                name=row.get("name_raw", ""),
                task_type=row.get("task_type", ""),
                op_type=row.get("op_type", "unknown"),
                accel_core=row.get("accelerator_core", ""),
                stream_id=row.get("stream_id", ""),
                start_us=safe_float(row.get("start_us")),
                end_us=safe_float(row.get("end_us")),
                duration_us=safe_float(row.get("duration_us")),
                wait_us=safe_float(row.get("wait_us")),
                pipeline=pipe,
                shape_signature=row.get("shape_signature", ""),
                op_roles=row.get("op_roles", ""),
                op_categories=row.get("op_categories", ""),
            ))
    return events


def _load_raw_kernel_details(root: Path) -> dict:
    """Read all original kernel_details.csv files referenced in source_index.json.

    Returns: {source_id: [row_dict, ...]} where index = row_idx (zero-based after header).
    """
    si_path = root / "source_index.json"
    if not si_path.exists():
        return {}
    si = json.loads(si_path.read_text())
    sources = si.get("sources", []) if isinstance(si, dict) else si
    by_source = {}
    for s in sources:
        if not isinstance(s, dict):
            continue
        if s.get("kind") != "kernel_details_csv":
            continue
        path = Path(s["path"])
        if not path.exists():
            print(f"  WARN: source path missing: {path}", file=sys.stderr)
            continue
        rows = []
        try:
            with path.open() as f:
                reader = csv.DictReader(f)
                for r in reader:
                    rows.append(r)
        except Exception as exc:
            print(f"  WARN: failed reading {path}: {exc}", file=sys.stderr)
            continue
        by_source[s["source_id"]] = rows
        print(f"  source {s['source_id'][:12]}… : {len(rows):,} rows ({path.name})", file=sys.stderr)
    return by_source


def _attach_raw_rows(events: list, raw_by_source: dict) -> int:
    hits = 0
    miss = 0
    for e in events:
        rows = raw_by_source.get(e.source_id)
        if not rows:
            miss += 1
            continue
        if 0 <= e.row_idx < len(rows):
            e.raw_row = rows[e.row_idx]
            hits += 1
        else:
            miss += 1
    print(f"  attached raw_row to {hits:,} events ({miss:,} miss)", file=sys.stderr)
    return hits


_COMM_NAME_HINTS = (
    "allreduce", "allgather", "reducescatter", "reduce_scatter",
    "broadcast", "alltoall", "all_to_all", "send", "recv",
    "dispatch", "combine",
)


def dedup_comm_aiv(events: list, iou_threshold: float = 0.9) -> int:
    """Mark AIV / mix_comm_aiv events that are dual-stream copies of an HCCL event.

    保守规则（两段都用）：
      A. op_type=mix_comm_aiv（dispatch/combine 等 fused kernel）必须与同 rank 的
         communication event 在时间上 IoU >= threshold → mark redundant。
      B. op_type=aiv 且 kernel 名命中通信关键词（allreduce/allgather/alltoall/...）
         且与同 rank 的 communication event 时间 IoU >= threshold → mark redundant。

    Rule B 防止"通信流上是 allreduce，计算流上是 aclnnAllReduce_xxx"被双重计入。
    AIV pipe 原始字段仍保留供分析。
    """
    if not events:
        return 0
    by_rank = defaultdict(list)
    for e in events:
        if e.op_type == "communication":
            by_rank[e.rank_id].append(e)
    for rid in by_rank:
        by_rank[rid].sort(key=lambda x: x.start_us)
    dedup = 0
    for e in events:
        if e.op_type == "mix_comm_aiv":
            pass  # rule A
        elif e.op_type == "aiv":
            nl = (e.name or "").lower()
            if not any(h in nl for h in _COMM_NAME_HINTS):
                continue
        else:
            continue
        cands = by_rank.get(e.rank_id, [])
        if not cands:
            continue
        for c in cands:
            if c.end_us < e.start_us:
                continue
            if c.start_us > e.end_us:
                break
            inter = max(0, min(c.end_us, e.end_us) - max(c.start_us, e.start_us))
            union = max(c.end_us, e.end_us) - min(c.start_us, e.start_us)
            iou = inter / union if union > 0 else 0.0
            if iou >= iou_threshold:
                e.redundant = True
                dedup += 1
                break
    return dedup


_RANK_EVENT_INDEX: dict[str, list] = {}
_RANK_EVENT_ROWS: dict[str, list] = {}


def _build_rank_event_index(events_by_row: list) -> None:
    """Build a per-rank, row-sorted event list + parallel row_idx array once."""
    global _RANK_EVENT_INDEX, _RANK_EVENT_ROWS
    _RANK_EVENT_INDEX = {}
    _RANK_EVENT_ROWS = {}
    bucket: dict[str, list] = defaultdict(list)
    for e in events_by_row:
        bucket[e.rank_id].append(e)
    for rid, lst in bucket.items():
        lst.sort(key=lambda x: x.row_idx)
        _RANK_EVENT_INDEX[rid] = lst
        _RANK_EVENT_ROWS[rid] = [e.row_idx for e in lst]


def events_in_row_range(events_by_row: list, row_start: int, row_end: int, rank_id: str | None = None) -> list:
    """`events_by_row` must be pre-sorted by row_idx.

    IMPORTANT: row_idx is per-source (per-rank), not globally unique. When pulling events
    for a specific step/layer/block segment, ALWAYS pass rank_id to filter out events from
    other ranks that happen to share the same row_idx range. Otherwise the resulting events
    span multiple ranks' absolute timestamps and the timeline range explodes to global scale.

    O(log N + k) when rank_id is provided and the index is pre-built. Falls back to O(N).
    """
    if rank_id is not None and rank_id in _RANK_EVENT_INDEX:
        lst = _RANK_EVENT_INDEX[rank_id]
        rows = _RANK_EVENT_ROWS[rank_id]
        lo = bisect.bisect_left(rows, row_start)
        hi = bisect.bisect_left(rows, row_end)
        return lst[lo:hi]
    out = []
    for e in events_by_row:
        if e.row_idx < row_start:
            continue
        if e.row_idx >= row_end:
            continue
        if rank_id is not None and e.rank_id != rank_id:
            continue
        out.append(e)
    return out


def load_bundle(root: Path) -> Bundle:
    b = Bundle(root=root)
    b.rank_summary = load_csv(root / "rank_summary.csv")
    b.step_summary = load_csv(root / "step_summary.csv")
    b.step_anatomy = load_csv(root / "step_anatomy.csv")
    b.step_class = load_csv(root / "step_class_summary.csv")
    b.layer_class = load_csv(root / "layer_class_summary.csv")
    b.block_class = load_csv(root / "block_class_summary.csv")
    b.operator_class = load_csv(root / "operator_class_summary.csv")
    b.hccl_class = load_csv(root / "hccl_class_summary.csv")
    b.hccl_op = load_csv(root / "hccl_op_summary.csv")
    findings = load_json(root / "diagnosis_findings.json") or []
    if isinstance(findings, dict):
        findings = findings.get("findings", [])
    b.findings = findings
    b.manifest = load_json(root / "manifest.json") or {}
    b.step_segments = _load_segments(root / "step_segments.json", "step_segments")
    b.layer_segments = _load_segments(root / "layer_segments.json", "layer_segments")
    b.block_segments = _load_segments(root / "block_segments.json", "block_segments")
    print(f"loading events from normalized_event_index.csv ...", file=sys.stderr)
    b.events = _load_events(root / "normalized_event_index.csv")
    b.events.sort(key=lambda e: e.row_idx)
    _build_rank_event_index(b.events)
    print(f"  loaded {len(b.events)} events", file=sys.stderr)
    n_dedup = dedup_comm_aiv(b.events)
    print(f"  marked {n_dedup} comm-shadow events as redundant (mix_comm_aiv + AIV ops with comm-name keywords vs HCCL events, IoU >= 0.9)", file=sys.stderr)
    print(f"loading raw kernel_details.csv (per source) ...", file=sys.stderr)
    raw_by_source = _load_raw_kernel_details(root)
    _attach_raw_rows(b.events, raw_by_source)
    return b


def render_head(title: str) -> str:
    css = (
        ":root{"
        f"--bg:{PALETTE['bg']};--bg-card:{PALETTE['bg_card']};--bg-card-alt:{PALETTE['bg_card_alt']};"
        f"--border:{PALETTE['border']};--text:{PALETTE['text']};--muted:{PALETTE['muted']};"
        f"--accent:{PALETTE['accent']};--warn:{PALETTE['warn']};--danger:{PALETTE['danger']};"
        f"--success:{PALETTE['success']};"
        "}"
        "*{box-sizing:border-box;}"
        "html,body{background:var(--bg);color:var(--text);margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Helvetica Neue',Arial,sans-serif;font-size:13px;line-height:1.45;}"
        "main{max-width:1400px;margin:0 auto;padding:24px 28px 64px;}"
        "h1{font-size:22px;margin:0 0 4px;font-weight:600;}"
        "h2{font-size:17px;margin:28px 0 10px;font-weight:600;padding-bottom:6px;border-bottom:1px solid var(--border);}"
        "h3{font-size:14px;margin:18px 0 8px;font-weight:600;}"
        ".muted{color:var(--muted);}"
        ".row{display:flex;flex-wrap:wrap;gap:12px;}"
        ".card{background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:14px 16px;}"
        ".kpi{flex:1 1 180px;min-width:180px;}"
        ".kpi .label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;}"
        ".kpi .value{font-size:22px;font-weight:600;margin-top:4px;}"
        ".kpi .sub{font-size:11px;color:var(--muted);margin-top:3px;}"
        ".badge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600;letter-spacing:.02em;}"
        ".b-real{background:rgba(63,185,80,.18);color:#56d364;}"
        ".b-companion{background:rgba(240,136,62,.18);color:#f0a065;}"
        ".b-mixed{background:rgba(212,168,255,.18);color:#d2a8ff;}"
        ".b-slow{background:rgba(248,81,73,.18);color:#ff7b72;}"
        ".b-fast{background:rgba(88,166,255,.18);color:#79c0ff;}"
        ".b-warn{background:rgba(240,136,62,.15);color:#f0a065;}"
        ".b-danger{background:rgba(248,81,73,.18);color:#ff7b72;}"
        ".b-success{background:rgba(63,185,80,.15);color:#56d364;}"
        "table{border-collapse:collapse;width:100%;font-size:12px;}"
        "th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--border);}"
        "th{font-weight:600;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.03em;background:var(--bg-card-alt);}"
        "td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;}"
        ".bar-cell{position:relative;height:18px;}"
        ".bar-cell .bar{position:absolute;left:0;top:0;height:100%;border-radius:2px;opacity:.65;}"
        ".bar-cell .label{position:relative;padding:0 6px;line-height:18px;font-variant-numeric:tabular-nums;}"
        ".stack-bar{display:flex;height:12px;border-radius:3px;overflow:hidden;background:var(--bg-card-alt);}"
        ".stack-bar>div{height:100%;}"
        "details{background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:10px 14px;margin-top:10px;}"
        "details>summary{cursor:pointer;font-weight:600;outline:none;list-style:none;padding-right:10px;}"
        "details>summary::-webkit-details-marker{display:none;}"
        "details>summary::before{content:'\\25B8';display:inline-block;width:14px;color:var(--muted);transition:transform .15s;}"
        "details[open]>summary::before{transform:rotate(90deg);}"
        ".nav{position:sticky;top:0;background:rgba(13,17,23,.95);z-index:10;padding:10px 0;margin:0 -28px 16px;padding-left:28px;padding-right:28px;border-bottom:1px solid var(--border);backdrop-filter:blur(6px);}"
        ".nav a{color:var(--muted);text-decoration:none;margin-right:16px;font-size:12px;}"
        ".nav a:hover{color:var(--text);}"
        ".scroll-x{overflow-x:auto;}"
        "svg .gridline{stroke:var(--border);stroke-width:.5;}"
        "svg .axis-text{fill:var(--muted);font-size:10px;font-family:inherit;}"
        "svg .rank-label{fill:var(--text);font-size:11px;font-family:inherit;font-weight:600;}"
        "svg .seg{cursor:pointer;}"
        "svg .seg:hover{stroke:var(--text);stroke-width:1.2;}"
        ".heatmap{display:grid;gap:2px;padding:4px;background:var(--bg-card-alt);border-radius:4px;}"
        ".heat-cell{height:22px;display:flex;align-items:center;justify-content:center;font-size:10px;color:#000;font-weight:600;font-variant-numeric:tabular-nums;border-radius:2px;}"
        ".op-row{display:flex;align-items:center;gap:10px;padding:3px 0;}"
        ".op-row .name{flex:0 0 220px;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}"
        ".op-row .bar-track{flex:1;height:14px;background:var(--bg-card-alt);border-radius:2px;position:relative;}"
        ".op-row .bar-fill{position:absolute;top:0;left:0;height:100%;border-radius:2px;}"
        ".op-row .v{flex:0 0 150px;text-align:right;font-size:11px;font-variant-numeric:tabular-nums;color:var(--muted);}"
        ".field{border-bottom:1px dotted var(--muted);cursor:help;}"
        ".op-card{background:var(--bg-card-alt);border:1px solid var(--border);border-radius:6px;padding:10px 12px;margin-bottom:10px;}"
        ".op-card-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;}"
        ".op-name{font-weight:600;font-size:13px;}"
        ".op-meta{display:flex;gap:14px;align-items:center;flex-wrap:wrap;}"
        ".op-meta>div:first-child{flex:1 1 320px;min-width:280px;}"
        ".op-shares{display:flex;gap:18px;font-size:11px;}"
        ".op-shares>div{display:flex;flex-direction:column;}"
        ".op-shares .v{font-size:14px;font-weight:600;color:var(--text);font-variant-numeric:tabular-nums;}"
        ".exec-wait-row{display:flex;align-items:center;gap:6px;padding:2px 0;}"
        ".ew-track{flex:1;height:10px;background:#0d1117;border-radius:2px;position:relative;}"
        ".ew-fill{position:absolute;left:0;top:0;height:100%;border-radius:2px;}"
        ".ew-v{flex:0 0 90px;text-align:right;font-size:11px;font-variant-numeric:tabular-nums;color:var(--muted);}"
        ".stage-row{display:flex;align-items:center;gap:6px;padding:2px 0;font-size:11px;}"
        ".stage-name{flex:0 0 130px;font-family:'SF Mono',Menlo,Consolas,monospace;font-size:11px;}"
        ".stage-bar-track{flex:1;height:8px;background:#0d1117;border-radius:1px;position:relative;}"
        ".stage-bar-fill{position:absolute;left:0;top:0;height:100%;border-radius:1px;}"
        ".stage-v{flex:0 0 80px;text-align:right;font-variant-numeric:tabular-nums;color:var(--muted);font-size:11px;}"
        ".op-card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(520px,1fr));gap:10px;}"
        ".layer-card{border:1px solid var(--border);border-radius:6px;padding:8px 12px;margin:10px 0;background:rgba(88,166,255,.04);}"
        ".layer-summary{cursor:pointer;font-size:14px;padding:4px 0;list-style:none;}"
        ".layer-summary::-webkit-details-marker{display:none}"
        ".block-card{border:1px solid var(--border);border-left:4px solid var(--accent);border-radius:4px;padding:6px 10px;margin:8px 0;background:#0d1117;}"
        ".block-summary{cursor:pointer;list-style:none;padding:4px 0;}"
        ".block-summary::-webkit-details-marker{display:none}"
        ".block-summary:hover{background:rgba(88,166,255,.08);border-radius:3px;padding:4px 6px;margin:-4px -6px 0 -6px;}"
        ".block-jump-bar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:8px 0 12px;padding:6px 10px;background:rgba(88,166,255,.05);border-radius:4px;}"
        ".block-jump{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:13px;cursor:pointer;text-decoration:none;font-size:12px;transition:transform .1s,filter .1s;}"
        ".block-jump:hover{transform:translateY(-1px);filter:brightness(1.3);}"
        ".single-step{border:1px solid var(--border);border-radius:5px;margin:8px 0;background:#0d1117;}"
        ".single-step[open]{border-left:3px solid var(--accent);}"
        ".step-summary{cursor:pointer;list-style:none;padding:10px 14px;display:flex;align-items:center;gap:10px;font-size:13px;}"
        ".step-summary::-webkit-details-marker{display:none}"
        ".step-summary:hover{background:rgba(88,166,255,.06);}"
        ".step-summary::before{content:'▶';margin-right:6px;color:var(--muted);transition:transform .15s;display:inline-block;font-size:9px;}"
        ".single-step[open] .step-summary::before{transform:rotate(90deg);}"
        ".step-body{padding:6px 14px 14px;border-top:1px solid var(--border);}"
        ".phase-bar{display:flex;height:14px;border-radius:3px;overflow:hidden;box-shadow:inset 0 0 0 1px var(--border);}"
        ".phase-bar > div{height:100%;}"
        ".deep-jump{display:inline-block;padding:6px 14px;background:rgba(88,166,255,.15);border:1px solid var(--accent);border-radius:4px;color:var(--accent);text-decoration:none;font-size:12px;cursor:pointer;}"
        ".deep-jump:hover{background:rgba(88,166,255,.25);}"
        ".bubble-axis{padding:14px 8px 6px;background:#0d1117;border:1px solid var(--border);border-radius:4px;margin-top:6px}"
        ".bt-evt{shape-rendering:crispEdges}"
        ".bt-evt:hover{stroke:#fff;stroke-width:0.5}"
        ".bt-bubble{pointer-events:auto;}"
        ".legend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:11px;color:var(--muted);}"
        ".jump-target{cursor:pointer;text-decoration:underline;text-decoration-style:dotted;text-decoration-color:var(--accent);}"
        ".jump-target:hover{color:var(--accent);}"
        ".chip{display:inline-block;padding:1px 7px;background:#0d1117;border:1px solid var(--border);border-radius:9px;font-size:10.5px;color:var(--muted);}"
        ".shape-preview{margin:6px 0 8px;padding:6px 8px;background:#0d1117;border:1px solid var(--border);border-radius:4px;font-size:11px;font-family:'SF Mono',Menlo,Consolas,monospace;overflow-x:auto;white-space:nowrap;}"
        ".shape-preview code{color:#79c0ff;}"
        ".ir-signature{margin:8px 0;background:#0d1117;border:1px solid var(--border);border-radius:4px;padding:8px 12px;font-family:'SF Mono',Menlo,Consolas,monospace;font-size:11.5px;}"
        ".ir-head{display:flex;align-items:center;gap:4px;}"
        ".ir-fname{color:#d2a8ff;font-weight:600;}"
        ".ir-tail{margin:2px 0;}"
        ".ir-block{padding-left:18px;}"
        ".ir-row{display:flex;align-items:baseline;gap:6px;padding:1px 0;flex-wrap:wrap;}"
        ".ir-undef{opacity:0.5}"
        ".ir-pname{color:#8b949e;min-width:42px;}"
        ".ir-colon{color:#8b949e;}"
        ".ir-dtype{display:inline-block;padding:0 6px;background:rgba(88,166,255,.15);color:#79c0ff;border-radius:2px;font-size:10.5px;font-weight:600;}"
        ".ir-fmt{display:inline-block;padding:0 6px;background:rgba(240,136,62,.15);color:#f0a065;border-radius:2px;font-size:10.5px;}"
        ".ir-shape{color:#7ee787;word-break:break-all;}"
        ".info-btn{display:inline-block;width:14px;height:14px;line-height:13px;text-align:center;background:#1c232c;border:1px solid var(--border);border-radius:50%;color:var(--accent);font-size:10px;cursor:pointer;margin-left:3px;user-select:none;font-weight:600;}"
        ".info-btn:hover{background:#30363d;color:#fff;}"
        ".info-popover{position:absolute;z-index:200;background:#161b22;border:1px solid var(--accent);border-radius:5px;padding:10px 12px;max-width:380px;min-width:260px;box-shadow:0 8px 24px rgba(0,0,0,.6);font-size:12px;line-height:1.55;color:var(--text);}"
        ".info-popover .pop-title{font-weight:600;color:var(--accent);margin-bottom:4px;font-family:'SF Mono',Menlo,Consolas,monospace;font-size:11.5px;}"
        ".info-popover .pop-close{position:absolute;top:4px;right:8px;cursor:pointer;color:var(--muted);font-size:14px;}"
        ".info-popover .pop-close:hover{color:var(--text);}"
        ".pipe-section{margin-top:10px;padding-top:8px;border-top:1px solid var(--border);}"
        ".stage-list{display:flex;flex-direction:column;gap:1px;}"
        ".stage-row{display:flex;align-items:center;gap:6px;padding:1px 0;font-size:11px;font-family:'SF Mono',Menlo,Consolas,monospace;}"
        ".stage-name{flex:0 0 110px;}"
        ".stage-bar-track{flex:1;height:9px;background:#0d1117;border-radius:1px;position:relative;min-width:80px;}"
        ".stage-bar-fill{position:absolute;left:0;top:0;height:100%;border-radius:1px;}"
        ".stage-ratio{flex:0 0 50px;text-align:right;font-variant-numeric:tabular-nums;color:var(--text);}"
        ".stage-v{flex:0 0 80px;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums;}"
        ".decision-block{margin-top:10px;padding:10px 12px;background:linear-gradient(90deg,rgba(248,81,73,.14),rgba(248,81,73,.04));border:1px solid rgba(248,81,73,.35);border-left:4px solid #f85149;border-radius:4px;font-size:12.5px;position:relative;}"
        ".decision-block .decision-icon{font-size:18px;margin-right:4px;}"
        ".decision-block code{background:rgba(255,255,255,.06);padding:1px 4px;border-radius:2px;color:#d2a8ff;}"
        ".util-section{margin-top:8px;padding:6px 8px;background:#0d1117;border-radius:3px;}"
        ".kv-row{display:flex;align-items:center;gap:6px;padding:1px 0;font-size:11px;font-family:'SF Mono',Menlo,Consolas,monospace;}"
        ".kv-k{flex:0 0 170px;}"
        ".kv-bar-track{flex:1;height:7px;background:#1c232c;border-radius:1px;position:relative;}"
        ".kv-bar-fill{position:absolute;left:0;top:0;height:100%;border-radius:1px;}"
        ".kv-v{flex:0 0 70px;text-align:right;color:var(--text);font-variant-numeric:tabular-nums;}"
        ".raw-details{margin-top:10px;background:#0d1117;border:1px solid var(--border);padding:6px 10px;}"
        ".raw-details>summary{font-size:11.5px;color:var(--muted);}"
        ".raw-fields{font-size:10.5px;font-family:'SF Mono',Menlo,Consolas,monospace;margin-top:6px;}"
        ".raw-fields td{padding:2px 8px;border-bottom:1px solid #1c232c;}"
        ".raw-fields .raw-k{color:var(--muted);width:180px;}"
        ".raw-fields .raw-v{color:var(--text);word-break:break-all;}"
        ".ew-label{flex:0 0 60px;font-size:11px;}"
        ".timeline-viewport{position:relative;overflow:hidden;border:1px solid var(--border);border-radius:4px;background:#0d1117;cursor:grab;user-select:none;}"
        ".timeline-viewport.dragging{cursor:grabbing;}"
        ".timeline-viewport svg{display:block;width:100%;}"
        ".tl-grid{stroke:#30363d;stroke-width:0.5;vector-effect:non-scaling-stroke;}"
        ".tl-axis{fill:#8b949e;font-size:9.5px;font-family:inherit;}"
        ".tl-evt{cursor:pointer;}"
        ".tl-evt:hover{stroke:#fff;stroke-width:1;vector-effect:non-scaling-stroke;}"
        ".timeline-viewport{outline:none}"
        ".timeline-viewport:focus{box-shadow:inset 0 0 0 2px rgba(88,166,255,.4);}"
        ".tl-label-layer{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;overflow:hidden;}"
        ".tl-evt-html{position:absolute;font:600 11px 'SF Mono',Menlo,Consolas,monospace;color:#0d1117;line-height:14px;padding:0 4px;white-space:nowrap;overflow:hidden;text-overflow:clip;box-sizing:border-box;}"
        ".tl-hint{font-size:11px;color:var(--muted)}.tl-hint b{color:var(--text);font-weight:600}"
        ".tl-stream-label{position:absolute;left:6px;transform:translateY(-50%);font-size:10.5px;color:var(--text);background:rgba(13,17,23,.92);padding:1px 6px;border-radius:2px;pointer-events:none;font-weight:600;font-family:'SF Mono',Menlo,Consolas,monospace;}"
        ".tl-marquee{position:absolute;background:rgba(88,166,255,.15);border:1px solid var(--accent);pointer-events:none;}"
        ".timeline-ctrl{display:flex;gap:8px;align-items:center;font-size:11px;color:var(--muted);margin:6px 0;}"
        ".timeline-ctrl button{background:#1c232c;border:1px solid var(--border);color:var(--text);padding:2px 8px;border-radius:3px;cursor:pointer;font-size:11px;}"
        ".timeline-ctrl button:hover{background:#30363d;}"
        # ----- v7: SPA chrome + views -----
        ".view{display:none;}"
        ".view.active{display:block;}"
        ".app-chrome{position:sticky;top:0;z-index:50;background:rgba(13,17,23,.96);backdrop-filter:blur(6px);"
        "border-bottom:1px solid var(--border);padding:10px 28px;margin:-24px -28px 16px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;}"
        ".chrome-title{font-weight:600;font-size:14px;color:var(--text);}"
        ".chrome-meta{color:var(--muted);font-size:11px;}"
        ".breadcrumb{display:flex;align-items:center;gap:4px;font-size:12px;color:var(--muted);flex-wrap:wrap;}"
        ".breadcrumb .crumb{padding:3px 9px;border-radius:3px;cursor:pointer;border:1px solid transparent;}"
        ".breadcrumb .crumb:hover{background:var(--bg-card-alt);color:var(--text);}"
        ".breadcrumb .crumb.active{background:rgba(88,166,255,.18);color:var(--accent);cursor:default;border-color:rgba(88,166,255,.35);}"
        ".breadcrumb .sep{color:var(--muted);padding:0 2px;}"
        ".back-btn{background:#1c232c;border:1px solid var(--border);color:var(--text);padding:5px 12px;border-radius:3px;cursor:pointer;font-size:12px;}"
        ".back-btn:hover{background:#30363d;}"
        ".back-btn:disabled{opacity:.35;cursor:not-allowed;}"
        # KPI strip
        ".kpi-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;}"
        ".kpi-strip .kpi{background:var(--bg-card);border:1px solid var(--border);border-radius:5px;padding:10px 12px;}"
        # step Gantt jumpable rects
        ".gantt-svg rect.seg{cursor:pointer;transition:filter .1s;}"
        ".gantt-svg rect.seg:hover{filter:brightness(1.3);stroke:#fff;stroke-width:0.6;}"
        # kernel rollup table layout
        ".kernel-rollup{margin:6px 0;border:1px solid var(--border);border-radius:4px;overflow:hidden;}"
        ".kernel-row{display:grid;grid-template-columns:1.7fr 0.6fr 0.4fr 1.4fr 0.6fr 0.5fr;gap:8px;padding:5px 10px;border-bottom:1px solid var(--border);font-size:11.5px;align-items:center;}"
        ".kernel-row.head{font-weight:600;color:var(--muted);font-size:10.5px;text-transform:uppercase;background:var(--bg-card-alt);letter-spacing:.04em;}"
        ".kernel-row:last-child{border-bottom:none;}"
        ".kernel-row .name{font-family:'SF Mono',Menlo,Consolas,monospace;font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}"
        ".kernel-row .bar-host{position:relative;height:14px;background:#0d1117;border-radius:2px;border:1px solid #1c232c;}"
        ".kernel-row .bar-fill{position:absolute;left:0;top:0;height:100%;border-radius:2px;opacity:.7;}"
        ".kernel-row .bar-lbl{position:relative;line-height:14px;padding:0 6px;font-variant-numeric:tabular-nums;font-size:10.5px;}"
        # phase split overview (main/spec/tail/bubble)
        ".phase-split{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;}"
        ".phase-split .cell{padding:10px 12px;border-radius:4px;border:1px solid var(--border);background:#0d1117;}"
        ".phase-split .cell .name{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;}"
        ".phase-split .cell .val{font-size:18px;font-weight:600;margin-top:3px;}"
        ".phase-split .cell .sub{font-size:11px;color:var(--muted);margin-top:3px;}"
        ".phase-split .cell.main{border-left:3px solid #58a6ff;}"
        ".phase-split .cell.spec{border-left:3px solid #d2a8ff;}"
        ".phase-split .cell.tail{border-left:3px solid #f0883e;}"
        ".phase-split .cell.bubble{border-left:3px solid #f85149;}"
        # cross-rank compare table for L2
        ".xrank-row{display:grid;grid-template-columns:1.3fr 0.7fr 0.7fr 1.5fr 0.6fr;gap:8px;padding:6px 10px;border-bottom:1px solid var(--border);font-size:12px;align-items:center;}"
        ".xrank-row.head{background:var(--bg-card-alt);color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;font-weight:600;}"
        # L3 op-list rows
        ".op-list{display:flex;flex-direction:column;gap:1px;background:var(--bg-card);border:1px solid var(--border);border-radius:4px;}"
        ".op-list-row{display:grid;grid-template-columns:36px 1.4fr 0.55fr 0.55fr 0.6fr 0.5fr 0.55fr 26px;gap:8px;padding:6px 10px;border-bottom:1px solid var(--border);font-size:11.5px;align-items:center;cursor:pointer;}"
        ".op-list-row:hover{background:rgba(88,166,255,.08);}"
        ".op-list-row.head{cursor:default;background:var(--bg-card-alt);color:var(--muted);font-weight:600;font-size:10.5px;text-transform:uppercase;}"
        ".op-list-row .ix{color:var(--muted);font-family:'SF Mono',Menlo,Consolas,monospace;font-size:10.5px;}"
        ".op-list-row .nm{font-family:'SF Mono',Menlo,Consolas,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}"
        ".op-card-host{padding:0 10px 10px;}"
        ".op-card-host.hidden{display:none;}"
        # model guess pill
        ".model-pill{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;background:rgba(210,168,255,.12);border:1px solid rgba(210,168,255,.35);border-radius:4px;color:#d2a8ff;font-size:12px;}"
        ".model-pill .lbl{font-size:10px;text-transform:uppercase;letter-spacing:.04em;opacity:.7;}"
    )
    js = r"""
// === v7: SPA view navigation ===
window._viewHistory = ['view-l1'];
window.showView = function(id) {
    const dst = document.getElementById(id);
    if (!dst) return false;
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    dst.classList.add('active');
    if (window._viewHistory[window._viewHistory.length - 1] !== id) {
        window._viewHistory.push(id);
    }
    window.scrollTo({top: 0, behavior: 'auto'});
    updateBreadcrumb(id);
    updateBackButton();
    return true;
};
window.goBack = function() {
    if (window._viewHistory.length <= 1) return;
    window._viewHistory.pop();
    const prev = window._viewHistory[window._viewHistory.length - 1];
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    const dst = document.getElementById(prev);
    if (dst) dst.classList.add('active');
    window.scrollTo({top: 0, behavior: 'auto'});
    updateBreadcrumb(prev);
    updateBackButton();
};
function updateBackButton() {
    const btn = document.getElementById('back-btn');
    if (btn) btn.disabled = window._viewHistory.length <= 1;
}
function updateBreadcrumb(currentId) {
    const dst = document.getElementById(currentId);
    if (!dst) return;
    const level = parseInt(dst.dataset.level || '1', 10);
    const crumb = document.getElementById('breadcrumb');
    if (!crumb) return;
    let html = '<span class="crumb' + (level === 1 ? ' active' : '') + '" data-show="view-l1">总览 · L1</span>';
    if (level >= 2) {
        const l2id = dst.dataset.l2id || currentId;
        const l2title = dst.dataset.l2title || '步详情';
        html += '<span class="sep">›</span><span class="crumb' + (level === 2 ? ' active' : '') + '" data-show="' + l2id + '">' + l2title + '</span>';
    }
    if (level >= 3) {
        const l3title = dst.dataset.title || '局部';
        html += '<span class="sep">›</span><span class="crumb active">' + l3title + '</span>';
    }
    crumb.innerHTML = html;
}
document.addEventListener('click', function(e) {
    const t = e.target.closest('[data-show]');
    if (t) {
        e.preventDefault();
        window.showView(t.getAttribute('data-show'));
    }
});
document.addEventListener('keydown', function(e) {
    // Esc / Backspace to go back (but not when typing or popover open)
    if (e.key === 'Backspace' && (document.activeElement.tagName === 'BODY' || document.activeElement === null)) {
        if (window._viewHistory.length > 1) {
            e.preventDefault();
            window.goBack();
        }
    }
});

// op-card-host lazy reveal in L3
document.addEventListener('click', function(e) {
    const row = e.target.closest('.op-list-row[data-card-id]');
    if (!row) return;
    const id = row.getAttribute('data-card-id');
    const host = document.getElementById(id);
    if (host) {
        host.classList.toggle('hidden');
    }
});

// === jump-to-anchor (legacy `data-jump` support for existing op-card / popover) ===
document.addEventListener('click', function (e) {
    const t = e.target.closest('[data-jump]');
    if (!t) return;
    const id = t.getAttribute('data-jump');
    const dst = document.getElementById(id);
    if (dst) {
        dst.scrollIntoView({behavior: 'smooth', block: 'start'});
        dst.setAttribute('open', '');
        dst.style.boxShadow = '0 0 0 2px var(--accent)';
        setTimeout(() => { dst.style.boxShadow = ''; }, 1400);
    }
});

// === sortable tables ===
document.querySelectorAll('th.sortable').forEach(th => {
    th.style.cursor = 'pointer';
    th.addEventListener('click', () => {
        const table = th.closest('table');
        const idx = Array.from(th.parentNode.children).indexOf(th);
        const numeric = th.classList.contains('num');
        const dir = th.dataset.sort === 'asc' ? -1 : 1;
        th.dataset.sort = dir === 1 ? 'asc' : 'desc';
        const rows = Array.from(table.querySelectorAll('tbody tr'));
        rows.sort((a, b) => {
            const av = a.children[idx].textContent.trim().replace(/[,%]/g, '');
            const bv = b.children[idx].textContent.trim().replace(/[,%]/g, '');
            if (numeric) return ((parseFloat(av) || 0) - (parseFloat(bv) || 0)) * dir;
            return av.localeCompare(bv) * dir;
        });
        const tb = table.querySelector('tbody');
        rows.forEach(r => tb.appendChild(r));
    });
});

// === Chrome-tracing-style zoomable timeline ===
function setupTimeline(vp) {
    const svg = vp.querySelector('svg');
    if (!svg) return;
    const orig = vp.getAttribute('data-orig-vb').split(' ').map(Number);
    const vbH = parseFloat(vp.getAttribute('data-vb-h') || orig[3]);
    const displayH = parseFloat(vp.getAttribute('data-display-h') || vbH);
    const status = document.getElementById(vp.id + '-status');
    const labelLayer = vp.querySelector('.tl-label-layer');
    const metaScript = vp.querySelector('script.tl-evt-meta');
    // event metadata: [x0_vb, w_vb, y_vb, h_vb, short_name]
    const evtMeta = metaScript ? JSON.parse(metaScript.textContent) : [];
    // HTML labels: create lazily; we keep a fixed-size pool of <span> reused on each redraw
    // (to avoid making thousands of DOM nodes when only ~few hundred fit on screen).
    // For simplicity: pre-create one span per event but only update style on visible ones.
    const labelEls = [];
    for (let i = 0; i < evtMeta.length; i++) {
        const m = evtMeta[i];
        const s = document.createElement('div');
        s.className = 'tl-evt-html';
        s.textContent = m[4] || '';
        s.style.display = 'none';
        labelLayer.appendChild(s);
        labelEls.push(s);
    }
    const updateLabels = () => {
        const vb = svg.viewBox.baseVal;
        const rect = vp.getBoundingClientRect();
        if (!rect.width) return;
        // pixels per viewBox unit (X), Y separately based on viewport height
        const ppu = rect.width / vb.width;
        const ppuY = rect.height / vbH;
        const minPxWidth = 18; // hide labels narrower than this
        for (let i = 0; i < labelEls.length; i++) {
            const m = evtMeta[i];
            const wPx = m[1] * ppu;
            const xPx = (m[0] - vb.x) * ppu;
            // cull off-screen
            if (xPx + wPx < 0 || xPx > rect.width || wPx < minPxWidth) {
                if (labelEls[i].style.display !== 'none') labelEls[i].style.display = 'none';
                continue;
            }
            const yPx = m[2] * ppuY;
            const hPx = m[3] * ppuY;
            const el = labelEls[i];
            el.style.display = 'block';
            el.style.left = xPx + 'px';
            el.style.top = (yPx + hPx / 2 - 7) + 'px';
            el.style.width = wPx + 'px';
            el.style.height = '14px';
        }
    };
    const updateStatus = () => {
        const vb = svg.viewBox.baseVal;
        const zoom = orig[2] / vb.width;
        if (status) status.textContent = `zoom ${zoom.toFixed(2)}× · view ${(vb.width/1000).toFixed(2)}ms wide @ x=${(vb.x/1000).toFixed(2)}ms`;
        updateLabels();
    };
    vp._tlRedraw = updateStatus;
    setTimeout(updateStatus, 0);

    // === Chrome-tracing-style interaction ===
    // Mouse wheel: zoom centered on cursor X
    // Mouse drag (left button, no shift): pan
    // Keyboard W/S: zoom in/out (center = current view center)
    // Keyboard A/D: pan left/right
    // dblclick: reset
    const _zoom = (factorIn, screenPxCenter) => {
        const vb = svg.viewBox.baseVal;
        const rect = vp.getBoundingClientRect();
        const px = (typeof screenPxCenter === 'number') ? (screenPxCenter / rect.width) : 0.5;
        const cursorVB = vb.x + px * vb.width;
        let newW = vb.width * factorIn;
        newW = Math.max(20, Math.min(orig[2] * 2, newW));
        vb.x = cursorVB - px * newW;
        vb.width = newW;
        updateStatus();
    };
    const _pan = (frac) => {
        const vb = svg.viewBox.baseVal;
        vb.x += vb.width * frac;
        updateStatus();
    };
    vp._tlZoom = _zoom;
    vp._tlPan = _pan;

    vp.addEventListener('wheel', (e) => {
        e.preventDefault();
        const rect = vp.getBoundingClientRect();
        const factor = e.deltaY < 0 ? 0.82 : 1.22;
        _zoom(factor, e.clientX - rect.left);
    }, {passive: false});

    let dragging = false, lastX = 0;
    vp.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        dragging = true;
        lastX = e.clientX;
        vp.classList.add('dragging');
        vp.focus();
    });
    window.addEventListener('mouseup', () => {
        dragging = false;
        vp.classList.remove('dragging');
    });
    window.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const vb = svg.viewBox.baseVal;
        const rect = vp.getBoundingClientRect();
        if (!rect.width) return;
        const dx = (e.clientX - lastX) / rect.width * vb.width;
        vb.x -= dx;
        lastX = e.clientX;
        updateStatus();
    });
    vp.addEventListener('dblclick', () => {
        const vb = svg.viewBox.baseVal;
        vb.x = orig[0]; vb.width = orig[2];
        updateStatus();
    });

    // Chrome-tracing W/S/A/D keys (only when timeline is focused or mouse is over it)
    let mouseInside = false;
    vp.addEventListener('mouseenter', () => { mouseInside = true; });
    vp.addEventListener('mouseleave', () => { mouseInside = false; });
    vp.addEventListener('keydown', (e) => {
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        const k = e.key.toLowerCase();
        if (k === 'w') { _zoom(0.82); e.preventDefault(); }
        else if (k === 's') { _zoom(1.22); e.preventDefault(); }
        else if (k === 'a') { _pan(-0.1); e.preventDefault(); }
        else if (k === 'd') { _pan(0.1); e.preventDefault(); }
        else if (k === '0' || k === '=' || k === 'escape') {
            const vb = svg.viewBox.baseVal;
            vb.x = orig[0]; vb.width = orig[2];
            updateStatus();
            e.preventDefault();
        }
    });
    // also accept global W/S/A/D when mouse hovers (no focus needed)
    window.addEventListener('keydown', (e) => {
        if (!mouseInside) return;
        if (document.activeElement && document.activeElement.tagName === 'INPUT') return;
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        const k = e.key.toLowerCase();
        if (k === 'w') { _zoom(0.82); e.preventDefault(); }
        else if (k === 's') { _zoom(1.22); e.preventDefault(); }
        else if (k === 'a') { _pan(-0.1); e.preventDefault(); }
        else if (k === 'd') { _pan(0.1); e.preventDefault(); }
    });
}

window.tlZoom = (id, factor) => {
    const vp = document.getElementById(id);
    if (vp && vp._tlZoom) vp._tlZoom(factor);
};
window.tlReset = (id) => {
    const vp = document.getElementById(id);
    if (!vp) return;
    const svg = vp.querySelector('svg');
    const vb = svg.viewBox.baseVal;
    const orig = vp.getAttribute('data-orig-vb').split(' ').map(Number);
    vb.x = orig[0]; vb.width = orig[2];
    if (vp._tlRedraw) vp._tlRedraw();
};

// Setup all timelines after DOM is ready
function setupAllTimelines() {
    document.querySelectorAll('.timeline-viewport').forEach(setupTimeline);
}
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setupAllTimelines);
} else {
    setupAllTimelines();
}

// === click-based info popover ===
window._fieldDocs = __FIELD_DOCS_PLACEHOLDER__;
let _openPopover = null;
function _closePopover() {
    if (_openPopover) {
        _openPopover.remove();
        _openPopover = null;
    }
}
function _openFieldPopover(anchor) {
    _closePopover();
    const key = anchor.getAttribute('data-doc-key');
    const doc = (window._fieldDocs || {})[key] || '(无字段说明)';
    const pop = document.createElement('div');
    pop.className = 'info-popover';
    pop.innerHTML = '<span class="pop-close">×</span>' +
        '<div class="pop-title">' + key.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</div>' +
        '<div>' + doc.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</div>';
    document.body.appendChild(pop);
    // position near anchor
    const r = anchor.getBoundingClientRect();
    const w = pop.offsetWidth;
    const h = pop.offsetHeight;
    let left = r.left + window.scrollX;
    if (left + w > window.innerWidth - 12) left = window.innerWidth - w - 12;
    let top = r.bottom + 6 + window.scrollY;
    if (r.bottom + h > window.innerHeight - 12) top = r.top + window.scrollY - h - 6;
    pop.style.left = Math.max(8, left) + 'px';
    pop.style.top = Math.max(8, top) + 'px';
    pop.querySelector('.pop-close').addEventListener('click', _closePopover);
    _openPopover = pop;
}
document.addEventListener('click', (e) => {
    const btn = e.target.closest('.info-btn');
    if (btn) {
        e.stopPropagation();
        if (_openPopover && _openPopover.dataset.anchor === btn.getAttribute('data-doc-key')) {
            _closePopover();
        } else {
            _openFieldPopover(btn);
        }
        return;
    }
    if (_openPopover && !e.target.closest('.info-popover')) _closePopover();
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') _closePopover();
});
"""
    pattern_defs = (
        '<svg width="0" height="0" style="position:absolute"><defs>'
        '<pattern id="bubble-pattern" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">'
        '<rect width="6" height="6" fill="transparent"/>'
        '<line x1="0" y1="0" x2="0" y2="6" stroke="rgba(248,81,73,.55)" stroke-width="2"/>'
        '</pattern></defs></svg>'
    )
    # v7: SPA chrome (back button + breadcrumb) — content injected per-render
    nav = (
        '<div class="app-chrome">'
        '<button id="back-btn" class="back-btn" onclick="window.goBack()" disabled>← 上一级</button>'
        '<div class="breadcrumb" id="breadcrumb">'
        '<span class="crumb active" data-show="view-l1">总览 · L1</span>'
        '</div>'
        f'<span class="chrome-title">{html.escape(title)}</span>'
        '<span class="chrome-meta">Backspace 返回 · 点击 step / layer 进入下一级</span>'
        '</div>'
    )
    field_docs_json = json.dumps(FIELD_DOC, ensure_ascii=False)
    js_filled = js.replace("__FIELD_DOCS_PLACEHOLDER__", field_docs_json)
    return (
        '<!doctype html><html lang="zh-cn"><head><meta charset="utf-8"><title>'
        + html.escape(title)
        + '</title><style>'
        + css
        + '</style></head><body>'
        + pattern_defs
        + '<main>'
        + nav
        + '<script>'
        + js_filled
        + '</script>'
    )


def render_foot():
    return "</main></body></html>"


def classify_workload(b: Bundle, rid: str):
    steps = [s for s in b.step_summary if s["rank_id"] == rid]
    if not steps:
        return ("b-mixed", "no data")
    dummy = sum(1 for s in steps if s.get("step_family") == "moe_or_dummy_workload")
    real = sum(1 for s in steps if s.get("step_family") == "attention_moe_workload")
    other = len(steps) - dummy - real
    if dummy >= 0.5 * len(steps) and real <= 0.2 * len(steps):
        return ("b-companion", f"companion · {dummy}/{len(steps)} dummy")
    if dummy >= 0.2 * len(steps):
        return ("b-mixed", f"mixed · {dummy} dummy / {real} real / {other} other")
    return ("b-real", f"real · {real}/{len(steps)} attention+moe")


def info_btn(doc_key: str) -> str:
    """Standalone ⓘ button referencing a field doc by key."""
    if doc_key not in FIELD_DOC:
        return ""
    return f'<span class="info-btn" data-doc-key="{html.escape(doc_key)}" title="点击查看字段说明">ⓘ</span>'


def pick_bound_stage(pipe: dict) -> str:
    """Return name of the dominant pipeline stage, ignoring aggregate aicore/aiv."""
    candidates = AIC_STAGES + AIV_STAGES
    best = None
    bestv = 0.0
    for k in candidates:
        v = safe_float(pipe.get(k))
        if v > bestv:
            best = k
            bestv = v
    return best or ""


def _split_semi(value: str) -> list[str]:
    # CANN kernel_details.csv: Input Shapes / Input Data Types / Input Formats fields are
    # ';'-separated lists; shape field may be wrapped in '"..."' to escape inner semicolons
    # when csv reader parses the cell. Empty tokens (between consecutive ';') represent
    # undefined inputs — preserve them so input index N aligns across shape/dtype/format.
    if not value:
        return []
    v = value.strip()
    # Strip outer quote wrappers (single or triple)
    while len(v) >= 2 and v.startswith('"') and v.endswith('"'):
        v = v[1:-1]
        if not v:
            break
    if not v:
        return []
    return [tok.strip() for tok in v.split(';')]


def _format_shape(s: str) -> str:
    """Render a single tensor shape string like '1,3,128,128' as '[1,3,128,128]'."""
    s = s.strip().strip('"').strip()
    if not s:
        return ""
    if s.startswith("[") and s.endswith("]"):
        return s
    # CANN sometimes uses ',' or ';' inside one shape (rare); we keep it simple
    return f"[{s}]"


def _render_op_signature(e: Event, short_name: str) -> str:
    """Render input/output tensors one-per-row in a pyfunc-like signature.

    Reads ';'-separated lists from raw kernel_details (Input/Output Shapes/Data Types/Formats).
    Empty tokens represent undefined inputs (e.g. optional inputs in FusedInferAttentionScore).
    No truncation.
    """
    raw = e.raw_row or {}
    in_shapes = _split_semi(raw.get("Input Shapes", ""))
    in_dtypes = _split_semi(raw.get("Input Data Types", ""))
    in_formats = _split_semi(raw.get("Input Formats", ""))
    out_shapes = _split_semi(raw.get("Output Shapes", ""))
    out_dtypes = _split_semi(raw.get("Output Data Types", ""))
    out_formats = _split_semi(raw.get("Output Formats", ""))
    if not (in_shapes or out_shapes):
        return ""

    def row(idx, shape, dtype, fmt, kind):
        # treat undefined inputs distinctly
        is_undef = (
            (not shape or shape in ("", "()", "[]"))
            and (not dtype or dtype in ("DT_UNDEFINED", "UNDEFINED"))
        )
        if is_undef:
            return (
                f'<div class="ir-row ir-undef">'
                f'<span class="ir-pname">{kind}_{idx}</span>'
                f'<span class="ir-colon">:</span>'
                f'<span class="muted" style="font-style:italic">undefined</span>'
                f'</div>'
            )
        chips = []
        if dtype and dtype != "DT_UNDEFINED":
            chips.append(f'<span class="ir-dtype">{html.escape(dtype)}</span>')
        if fmt and fmt not in ("ND", "NULL"):
            chips.append(f'<span class="ir-fmt">{html.escape(fmt)}</span>')
        chip_html = "".join(chips)
        return (
            f'<div class="ir-row">'
            f'<span class="ir-pname">{kind}_{idx}</span>'
            f'<span class="ir-colon">:</span>'
            f'{chip_html}'
            f'<code class="ir-shape">{html.escape(_format_shape(shape))}</code>'
            f'</div>'
        )

    n_in = max(len(in_shapes), len(in_dtypes), len(in_formats))
    in_rows = []
    for i in range(n_in):
        sh = in_shapes[i] if i < len(in_shapes) else ""
        dt = in_dtypes[i] if i < len(in_dtypes) else ""
        fm = in_formats[i] if i < len(in_formats) else ""
        in_rows.append(row(i, sh, dt, fm, "in"))
    n_out = max(len(out_shapes), len(out_dtypes), len(out_formats))
    out_rows = []
    for i in range(n_out):
        sh = out_shapes[i] if i < len(out_shapes) else ""
        dt = out_dtypes[i] if i < len(out_dtypes) else ""
        fm = out_formats[i] if i < len(out_formats) else ""
        out_rows.append(row(i, sh, dt, fm, "out"))

    n_defined = sum(1 for r in in_rows if "ir-undef" not in r)
    in_summary = f'{n_defined} input{"s" if n_defined != 1 else ""}'
    if n_in > n_defined:
        in_summary += f' <span class="muted">(+{n_in - n_defined} undefined)</span>'

    sig_header = (
        f'<div class="ir-head">'
        f'<span class="ir-fname">{html.escape(short_name)}</span>'
        f'<span class="muted">(</span> '
        f'<span class="muted" style="font-size:10.5px">{in_summary}</span>'
        f'</div>'
    )
    in_block = '<div class="ir-block">' + "".join(in_rows) + '</div>'
    sig_arrow = '<div class="ir-tail"><span class="muted">) →</span></div>'
    out_block = '<div class="ir-block">' + "".join(out_rows) + '</div>'

    return f'<div class="ir-signature">{sig_header}{in_block}{sig_arrow}{out_block}</div>'


def _stage_ratio_value(raw_row: dict, stage_time_field: str) -> float | None:
    """Get CANN-reported ratio for a pipeline stage (returns None if missing)."""
    ratio_field = STAGE_RATIO_FIELD.get(stage_time_field)
    if not ratio_field or not raw_row:
        return None
    raw_key = ratio_field if ratio_field in raw_row else None
    if raw_key is None:
        for k in raw_row.keys():
            if k.startswith(ratio_field):
                raw_key = k
                break
    if raw_key is None:
        return None
    v = raw_row.get(raw_key)
    try:
        return float(v)
    except Exception:
        return None


def _decide_bound_stage(e: Event) -> tuple[str, float | None, str]:
    """Pick decision stage using CANN ratio fields first (preferred), fall back to absolute time.

    Returns (stage_time_field_name, ratio_value_0to1, decision_basis_short)
    """
    # ratio-based first (only stages with ratio present)
    candidates = []
    for stage in AIC_STAGES + AIV_STAGES:
        if e.op_type == "aic" and stage in AIV_STAGES:
            continue
        if e.op_type == "aiv" and stage in AIC_STAGES:
            continue
        r = _stage_ratio_value(e.raw_row, stage)
        if r is not None:
            candidates.append((stage, r))
    if candidates:
        candidates.sort(key=lambda kv: -kv[1])
        s, r = candidates[0]
        return (s, r, "ratio")
    # fall back to absolute time
    s = pick_bound_stage(e.pipeline)
    return (s, None, "absolute_time")


def render_operator_card(e: Event, layer_total_us: float,
                          card_id: str = "",
                          step_busy_us: float = 0.0,
                          kernel_layer_union_us: dict | None = None,
                          kernel_layer_count: "Counter | None" = None,
                          kernel_step_union_us: dict | None = None,
                          kernel_step_count: "Counter | None" = None) -> str:
    """3-tier operator card: highlights / pipeline ratios / raw 46-field dump.

    Scope is strictly current rank · current step · current layer (per user
    spec). All "share" metrics use union(active time) denominators so we never
    double-count concurrent AIC+AIV events.
    """
    short = short_op_name(e.name)
    op_type_color = OP_TYPE_COLOR.get(e.op_type, "#8b949e")
    bound, bound_ratio, decision_basis = _decide_bound_stage(e)
    bound_family = STAGE_FAMILY.get(bound, "unknown")
    bf_color = BOUND_FAMILY_COLOR.get(bound_family, "#8b949e")

    redundant_chip = '<span class="badge b-warn" title="本 event 与同时段的 HCCL 通信算子时间重叠 ≥ 0.9，已在累加里跳过（AIV 字段仍保留供分析）">redundant</span>' if e.redundant else ""

    # exec / wait bars
    total_with_wait = e.duration_us + e.wait_us
    bar_max = max(total_with_wait, 1.0)
    exec_pct = e.duration_us / bar_max * 100
    wait_pct = e.wait_us / bar_max * 100
    wait_ratio = e.wait_us / e.duration_us if e.duration_us > 0 else 0.0
    host_warn = wait_ratio > 0.3

    # 本次 (single call) 占 layer active union
    self_layer_pct = (e.duration_us / layer_total_us * 100) if layer_total_us > 0 else 0
    # 本类 kernel 在 layer 内累计 union 占 layer active union
    klayer_us = (kernel_layer_union_us or {}).get(short, e.duration_us)
    klayer_n = (kernel_layer_count or {}).get(short, 1)
    klayer_pct = (klayer_us / layer_total_us * 100) if layer_total_us > 0 else 0
    # 本类 kernel 在 step 内累计 union 占 step active union
    kstep_us = (kernel_step_union_us or {}).get(short, e.duration_us)
    kstep_n = (kernel_step_count or {}).get(short, 1)
    kstep_pct = (kstep_us / step_busy_us * 100) if step_busy_us > 0 else 0

    # which stages to show
    if e.op_type in ("mix_cv", "mix_comm_aiv"):
        stages = AIC_STAGES + AIV_STAGES
    elif e.op_type == "aic":
        stages = AIC_STAGES
    elif e.op_type == "aiv":
        stages = AIV_STAGES
    else:
        stages = AIC_STAGES + AIV_STAGES

    # Pipeline ratio rows (CANN-reported)
    stage_rows = []
    if e.op_type != "communication":
        # decision stage first
        ordered = ([bound] if bound and bound in stages else []) + [s for s in stages if s != bound]
        for s in ordered:
            t_us = safe_float(e.pipeline.get(s))
            r = _stage_ratio_value(e.raw_row, s)
            is_decision = (s == bound)
            family = STAGE_FAMILY.get(s, "unknown")
            color = BOUND_FAMILY_COLOR.get(family, "#8b949e")
            marker = '<span style="color:#f85149;margin-right:2px;font-weight:600">🔥</span>' if is_decision else '<span style="display:inline-block;width:14px"></span>'
            label_style = "color:#f85149;font-weight:600" if is_decision else ""
            ratio_field = STAGE_RATIO_FIELD.get(s, "")
            ratio_label = (f"{r*100:.1f}%" if r is not None else "—")
            ratio_bar_w = (r * 100) if r is not None else 0
            stage_rows.append(
                '<div class="stage-row">'
                f'{marker}<span class="stage-name" style="{label_style}">{s.replace("_time","")}</span>'
                f'{info_btn(s)}'
                f'<div class="stage-bar-track">'
                f'<div class="stage-bar-fill" style="width:{ratio_bar_w:.1f}%;background:{color}"></div>'
                f'</div>'
                f'<span class="stage-ratio">{ratio_label}</span>'
                f'{info_btn(ratio_field) if r is not None else ""}'
                f'<span class="stage-v">{t_us:,.2f} μs</span>'
                '</div>'
            )
        pipeline_html = '<div class="stage-list">' + "".join(stage_rows) + '</div>'
    else:
        pipeline_html = '<div class="muted" style="font-size:11px;margin-top:6px">communication op — 无 AIC/AIV pipeline stage（全 0）</div>'

    # Decision narrative (with click ⓘ buttons for each technical term)
    if bound:
        if decision_basis == "ratio":
            ratio_pct_str = f"{bound_ratio*100:.1f}%"
            candidate_count = len([s for s in stages if _stage_ratio_value(e.raw_row, s) is not None])
            decision_note = (
                f'<div class="decision-block">'
                f'<span class="decision-icon">🔥</span> '
                f'<b>判定 bound_stage</b>{info_btn("bound_stage")} = '
                f'<span style="color:#f85149;font-weight:600;font-family:monospace">{bound.replace("_time","")}</span>'
                f' → <b>bound_family</b>{info_btn("bound_family")} = '
                f'<span class="badge" style="background:{bf_color}33;color:{bf_color}">{bound_family}</span>'
                f'<div class="muted" style="font-size:11.5px;margin-top:5px">'
                f'<b>依据</b>：CANN 报告的 <code>{STAGE_RATIO_FIELD.get(bound,"")}</code>{info_btn(STAGE_RATIO_FIELD.get(bound, ""))}'
                f' = <span style="color:#f0883e;font-weight:600">{ratio_pct_str}</span>'
                f'（在 {candidate_count} 个候选 stage 中 ratio 最高）'
                f'</div></div>'
            )
        else:
            t = safe_float(e.pipeline.get(bound))
            decision_note = (
                f'<div class="decision-block">'
                f'<span class="decision-icon">🔥</span> '
                f'<b>判定 bound_stage</b>{info_btn("bound_stage")} = '
                f'<span style="color:#f85149">{bound.replace("_time","")}</span> → '
                f'<b>bound_family</b>{info_btn("bound_family")} = '
                f'<span class="badge" style="background:{bf_color}33;color:{bf_color}">{bound_family}</span>'
                f'<div class="muted" style="font-size:11.5px;margin-top:5px">'
                f'<b>依据</b>：绝对耗时最大（<span style="color:#f0883e">{t:.2f}μs</span>，ratio 字段在 raw row 中缺失，退化判断）'
                f'</div></div>'
            )
    else:
        decision_note = ""

    # Utilization / icache miss summary (with click ⓘ)
    extra_rows = []
    for key in ("cube_utilization(%)", "aic_icache_miss_rate", "aiv_icache_miss_rate"):
        if key in e.raw_row and e.raw_row.get(key) not in (None, "", "N/A"):
            try:
                v = float(e.raw_row[key])
                display = (f"{v:.1f}%" if "%" in key else f"{v*100:.2f}%")
                extra_rows.append(
                    f'<div class="kv-row"><span class="kv-k">{html.escape(key)}</span>{info_btn(key)}'
                    f'<div class="kv-bar-track"><div class="kv-bar-fill" style="width:{min(v*100 if v<=1 else v, 100):.1f}%;background:#ffa657"></div></div>'
                    f'<span class="kv-v">{display}</span></div>'
                )
            except Exception:
                pass

    # Raw 46-field dump (tier 3) — with click ⓘ for documented fields
    raw_rows_html = []
    if e.raw_row:
        for f_key in RAW_KD_FIELDS:
            v = e.raw_row.get(f_key, "")
            if v in (None, "", "N/A"):
                v_html = '<span class="muted">—</span>'
            else:
                v_html = html.escape(str(v))
            has_doc = f_key in FIELD_DOC
            ibtn = info_btn(f_key) if has_doc else ""
            raw_rows_html.append(
                f'<tr><td class="raw-k">{html.escape(f_key)}{ibtn}</td><td class="raw-v">{v_html}</td></tr>'
            )
    raw_table = (
        '<table class="raw-fields">' + "".join(raw_rows_html) + '</table>'
        if raw_rows_html else
        '<div class="muted">无法 join 回原始 kernel_details.csv 行（source 缺失）</div>'
    )

    # Input/Output IR-style signature (no truncation; renders pyfunc-like)
    shape_preview = _render_op_signature(e, short)

    host_warn_chip = '<span class="badge b-warn" title="wait_us / duration_us > 30% → 该算子很可能 host bound 或上游同步等待">host bound suspected</span>' if host_warn else ""

    block_dim_chip = ""
    bd = e.raw_row.get("Block Dim", "")
    mbd = e.raw_row.get("Mix Block Dim", "")
    if bd and bd not in ("0", "", "N/A"):
        block_dim_chip = f'<span class="chip" title="{html.escape(FIELD_DOC.get("Block Dim",""))}">block_dim={html.escape(str(bd))}{f"/{html.escape(str(mbd))}" if mbd and mbd not in ("0","","N/A") else ""}</span>'

    return f"""
<div class="op-card" id="opcard-{card_id or e.event_id}">
  <div class="op-card-head">
    <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px">
      <span class="op-name" title="{html.escape(e.name)}">{html.escape(short)}</span>
      <span class="badge" style="background:{op_type_color}33;color:{op_type_color}">{html.escape(e.op_type)}</span>
      <span class="muted" style="font-size:11px" title="{html.escape(FIELD_DOC.get('stream_id',''))}">stream {html.escape(e.stream_id or '—')}</span>
      {block_dim_chip}
      {host_warn_chip}
      {redundant_chip}
    </div>
    <div class="muted" style="font-size:11px">{html.escape(e.task_type)}</div>
  </div>

  {shape_preview}

  <div class="op-meta">
    <div style="flex:1 1 320px;min-width:260px">
      <div class="exec-wait-row">
        <span class="muted ew-label">execution {info_btn("duration_us")}</span>
        <div class="ew-track"><div class="ew-fill" style="width:{exec_pct:.1f}%;background:#3fb950"></div></div>
        <span class="ew-v">{e.duration_us:,.2f} μs</span>
      </div>
      <div class="exec-wait-row">
        <span class="muted ew-label">wait {info_btn("wait_us")}</span>
        <div class="ew-track"><div class="ew-fill" style="width:{wait_pct:.1f}%;background:#f0883e"></div></div>
        <span class="ew-v">{e.wait_us:,.2f} μs  <span class="muted">({wait_ratio*100:.0f}% of exec)</span></span>
      </div>
    </div>
    <div class="op-shares">
      <div>
        <span class="muted">本次占 layer{info_btn("self_layer_pct")}</span>
        <span class="v">{self_layer_pct:.2f}%</span>
        <span class="muted" style="font-size:10px;display:block;margin-top:1px">{e.duration_us:,.1f} μs / {layer_total_us:,.0f} μs (layer active)</span>
      </div>
      <div>
        <span class="muted">本类累计占 layer{info_btn("klayer_pct")}</span>
        <span class="v">{klayer_pct:.2f}% <span class="muted" style="font-size:11px">({klayer_n}×)</span></span>
        <span class="muted" style="font-size:10px;display:block;margin-top:1px">{klayer_us:,.0f} μs / {layer_total_us:,.0f} μs (layer active)</span>
      </div>
      <div>
        <span class="muted">本类累计占 step{info_btn("kstep_pct")}</span>
        <span class="v">{kstep_pct:.2f}% <span class="muted" style="font-size:11px">({kstep_n}×)</span></span>
        <span class="muted" style="font-size:10px;display:block;margin-top:1px">{kstep_us:,.0f} μs / {step_busy_us:,.0f} μs (step active)</span>
      </div>
    </div>
  </div>

  <div class="pipe-section">
    <div class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px">
      Pipeline stages <span class="muted">· ratio (CANN-reported) + time · 🔥 = 判定 bound_stage</span>
    </div>
    {pipeline_html}
  </div>

  {decision_note}

  {('<div class="util-section">' + "".join(extra_rows) + '</div>') if extra_rows else ""}

  <details class="raw-details">
    <summary>📋 原始 kernel_details.csv 全 46 字段</summary>
    {raw_table}
  </details>
</div>
"""


_TIMELINE_COUNTER = [0]


# -----------------------------
# v7: SPA view renderers (L1 / L2 / L3)
# -----------------------------

def render_l1_view(b: "Bundle") -> str:
    """L1 总览：跨 rank 的 DP/EP 负载、快慢卡、陪跑判定、per-rank step Gantt."""
    # ---- KPI strip ----
    rank_count = len(b.rank_summary)
    step_count = sum(int(safe_float(r["step_count"])) for r in b.rank_summary)
    total_wall = sum(safe_float(r["wall_ms"]) for r in b.rank_summary) / max(rank_count, 1)
    ep = compute_ep_balance(b)
    comp = assess_companion_run(b)

    # Pre-compute values to avoid nested f-string quoting issues
    ep_avail = ep["available"]
    ep_p2m = ep["peak_to_mean"]
    ep_color = "#ff7b72" if (ep_avail and ep_p2m >= 1.10) else ("#3fb950" if ep_avail else "var(--muted)")
    ep_val = f"{ep_p2m:.2f}×" if ep_avail else "—"
    ep_sub = f"peak {ep['peak_us']/1000:.1f} ms / mean {ep['mean_us']/1000:.1f} ms" if ep_avail else "无 GroupedMatmul 事件"
    comp_color = "#f0a065" if comp["n_companion"] > 0 else "#3fb950"
    comp_msg = "存在 real ↔ dummy 错位" if comp["n_companion"] > 0 else "所有 rank 同步"
    findings_freq = (
        Counter(f.get("type", "?") for f in b.findings).most_common(1)[0][0]
        if b.findings else "—"
    )
    ep_info = info_btn("ep_peak_to_mean") if "ep_peak_to_mean" in FIELD_DOC else ""

    kpi_strip = (
        '<div class="kpi-strip">'
        f'<div class="kpi"><div class="label">参与 Rank</div><div class="value">{rank_count}</div>'
        f'<div class="sub">{step_count} step · 平均 wall {fmt_ms(total_wall)} ms / rank</div></div>'
        f'<div class="kpi"><div class="label">EP 峰均比 (GMM){ep_info}</div>'
        f'<div class="value" style="color:{ep_color}">{ep_val}</div>'
        f'<div class="sub">{ep_sub}</div></div>'
        f'<div class="kpi"><div class="label">DP 陪跑步数</div>'
        f'<div class="value" style="color:{comp_color}">{comp["n_companion"]} / {comp["n_total_aligned"]}</div>'
        f'<div class="sub">{comp_msg}</div></div>'
        f'<div class="kpi"><div class="label">Findings</div>'
        f'<div class="value">{len(b.findings)}</div>'
        f'<div class="sub">最频 {findings_freq}</div></div>'
        '</div>'
    )

    # ---- Cross-rank table (slow card / fast card / workload) ----
    rank_rows = b.rank_summary
    busy_mean = statistics.mean(safe_float(r["busy_union_ms"]) for r in rank_rows) if rank_rows else 0
    xrank_rows = []
    for r in sorted(rank_rows, key=lambda x: safe_float(x["busy_union_ms"]), reverse=True):
        busy = safe_float(r["busy_union_ms"])
        wall = safe_float(r["wall_ms"])
        underfeed = safe_float(r["underfeed_ratio"])
        diff = (busy - busy_mean) / busy_mean if busy_mean else 0
        if diff > 0.30:
            speed_badge = '<span class="badge b-slow">慢卡</span>'
        elif diff < -0.30:
            speed_badge = '<span class="badge b-fast">轻卡</span>'
        else:
            speed_badge = '<span class="badge b-success">normal</span>'
        wl_class, wl_label = classify_workload(b, r["rank_id"])
        gmm_per_rank = ep["by_rank"].get(r["rank_id"], 0.0) if ep["available"] else 0.0
        gmm_label = (f"{gmm_per_rank/1000:.1f} ms" if ep["available"] else "—")
        xrank_rows.append(
            "<tr>"
            f"<td><b>{html.escape(short_rank_label(r['rank_id']))}</b>"
            f"<div class='muted' style='font-size:10px'>{html.escape(r['rank_id'])}</div></td>"
            f"<td class='num'>{int(safe_float(r['step_count']))}</td>"
            f"<td class='num'>{fmt_ms(wall)}</td>"
            f"<td class='num'>{fmt_ms(busy)}</td>"
            f"<td class='num'>{diff*100:+.1f}%</td>"
            f"<td class='num'>{gmm_label}</td>"
            f"<td class='num'>{underfeed*100:.1f}%</td>"
            f"<td>{speed_badge}</td>"
            f"<td><span class='badge {wl_class}'>{html.escape(wl_label)}</span></td>"
            "</tr>"
        )

    cross_rank_html = (
        '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">跨 Rank 总览</h3>'
        '<div class="scroll-x"><table>'
        '<thead><tr><th>Rank</th><th class="num">Steps</th><th class="num">Wall ms</th>'
        '<th class="num">Busy ms</th><th class="num">Busy vs 均值</th>'
        f'<th class="num">GMM 总 ms{info_btn("ep_per_rank_gmm") if "ep_per_rank_gmm" in FIELD_DOC else ""}</th>'
        '<th class="num">Underfeed</th><th>Speed</th><th>Workload</th></tr></thead>'
        f'<tbody>{"".join(xrank_rows)}</tbody></table></div>'
        '<div class="muted" style="margin-top:6px;font-size:11px">'
        'Speed：busy 比组均值 ±30% 时报警 · Workload：real = attention+moe 占比 &gt; 80% · companion = ≥ 50% 步是 moe-only/dummy'
        '</div></div>'
    )

    # ---- EP imbalance detail (only when GMM available) ----
    ep_html = ""
    if ep["available"]:
        rid_sorted = sorted(ep["by_rank"], key=lambda r: -ep["by_rank"][r])
        rows = []
        max_v = ep["peak_us"]
        for rid in rid_sorted:
            v = ep["by_rank"][rid]
            deviation = (v - ep["mean_us"]) / ep["mean_us"] if ep["mean_us"] else 0
            color = "#ff7b72" if deviation > 0.10 else ("#3fb950" if abs(deviation) < 0.05 else "#79c0ff")
            bar_pct = (v / max_v * 100) if max_v else 0
            rows.append(
                "<tr>"
                f"<td><b>{html.escape(short_rank_label(rid))}</b></td>"
                f"<td class='num'>{v/1000:.2f}</td>"
                "<td class='bar-cell' style='min-width:160px'>"
                f"<div class='bar' style='width:{bar_pct:.1f}%;background:{color}'></div>"
                f"<div class='label'>{deviation*100:+.1f}% vs mean</div></td>"
                "</tr>"
            )
        ep_verdict = (
            '<span class="badge b-danger">EP imbalance</span>'
            if ep["peak_to_mean"] >= 1.10 else
            '<span class="badge b-success">EP balanced</span>'
        )
        ep_html = (
            '<div class="card" style="margin-top:14px">'
            f'<h3 style="margin-top:0">EP 负载（GroupedMatmul wall）· {ep_verdict}</h3>'
            '<div class="muted" style="font-size:11.5px;margin-bottom:6px">'
            f'峰均比 = max / mean = <b>{ep["peak_to_mean"]:.3f}</b> · spread = (max-min) / mean = <b>{ep["spread"]*100:.1f}%</b>'
            ' · 经验阈值：&gt; 1.10 视为 EP 不均（GroupedMatmul 是 MoE expert dispatch 的核心 kernel，每 rank 的 GMM 总耗时直接反映分到的 token 量）'
            '</div>'
            '<table>'
            '<thead><tr><th>Rank</th><th class="num">GMM 总耗时 ms</th><th>Deviation vs mean</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
            '</div>'
        )

    # ---- Companion run detail ----
    companion_html = ""
    if comp["n_companion"] > 0:
        rows = []
        for pair in comp["companion_rank_pairs"]:
            real_lbl = ", ".join(short_rank_label(r) for r in pair["real_ranks"])
            dummy_lbl = ", ".join(short_rank_label(r) for r in pair["dummy_ranks"])
            rows.append(
                "<tr>"
                f"<td><b>{html.escape(real_lbl)}</b></td>"
                f"<td><span class='muted'>陪跑</span> <b>{html.escape(dummy_lbl)}</b></td>"
                f"<td class='num'>{pair['count']}</td>"
                "</tr>"
            )
        companion_html = (
            '<div class="card" style="margin-top:14px">'
            '<h3 style="margin-top:0">DP 陪跑判定 · <span class="badge b-warn">存在错位</span></h3>'
            '<div class="muted" style="font-size:11.5px;margin-bottom:6px">'
            f'在 {comp["n_total_aligned"]} 个对齐 step 中，有 <b>{comp["n_companion"]}</b> 个 step 出现：部分 rank 跑真实数据（attention+moe / attention+dense），'
            '另一部分 rank 跑 moe-only / ffn-only / 空 dummy。这通常意味着 prefill 阶段或 schedule 不均。'
            '</div>'
            '<table>'
            '<thead><tr><th>真实数据 rank</th><th>陪跑 rank</th><th class="num">出现步数</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
            '</div>'
        )

    # ---- per-rank step Gantt (each step clickable → L2) ----
    gantt_html = _render_l1_gantt(b)

    return (
        '<section class="view active" id="view-l1" data-level="1">'
        f'{kpi_strip}'
        f'{cross_rank_html}'
        f'{ep_html}'
        f'{companion_html}'
        '<div style="margin-top:14px"><h3 style="margin:0 0 8px 0">每 Rank Step 时间线 · 点击任一 step 进入 L2</h3>'
        f'{gantt_html}'
        '</div>'
        '</section>'
    )


def _render_l1_gantt(b: "Bundle") -> str:
    """Reuse existing per-rank Gantt but make each step rect click → showView('view-l2-{seg_id}')."""
    if not b.step_summary:
        return '<div class="muted">无 step 数据</div>'
    by_rank = defaultdict(list)
    for r in b.step_summary:
        by_rank[r["rank_id"]].append(r)
    for rid in by_rank:
        by_rank[rid].sort(key=lambda x: safe_float(x["start_us"]))
    ranks = sorted(by_rank.keys())
    max_wall_ms = max((safe_float(r["wall_ms"]) for r in b.rank_summary), default=0.0)
    if max_wall_ms <= 0:
        return '<div class="muted">wall 数据缺失</div>'

    width = 1320
    margin_l = 130
    margin_r = 20
    row_h = 32
    gap = 5
    label_h = 24
    plot_w = width - margin_l - margin_r
    plot_h = label_h + len(ranks) * (row_h + gap)
    height = plot_h + 20

    def x_of(ms):
        return margin_l + (ms / max_wall_ms) * plot_w

    parts = []
    grid_step_ms = 2000
    for tick in range(0, int(max_wall_ms) + grid_step_ms, grid_step_ms):
        x = x_of(tick)
        parts.append(f'<line class="gridline" x1="{x:.1f}" y1="{label_h}" x2="{x:.1f}" y2="{plot_h}"/>')
        parts.append(f'<text class="axis-text" x="{x:.1f}" y="{label_h-6}" text-anchor="middle">{tick/1000:.1f}s</text>')

    for ri, rid in enumerate(ranks):
        row_top = label_h + ri * (row_h + gap)
        parts.append(
            f'<text class="rank-label" x="{margin_l-8:.1f}" y="{row_top + row_h/2 + 4:.1f}" text-anchor="end">{html.escape(short_rank_label(rid))}</text>'
        )
        parts.append(
            f'<rect x="{margin_l}" y="{row_top}" width="{plot_w}" height="{row_h}" fill="#1c232c" rx="3"/>'
        )
        rank_start = min(safe_float(s["start_us"]) for s in by_rank[rid]) if by_rank[rid] else 0
        for seg in by_rank[rid]:
            t0 = (safe_float(seg["start_us"]) - rank_start) / 1000.0
            t1 = (safe_float(seg["end_us"]) - rank_start) / 1000.0
            x0 = x_of(t0)
            x1 = x_of(t1)
            w = max(1.0, x1 - x0)
            family = seg.get("step_family", "")
            scid = seg.get("step_class_id", "")
            color = class_color(family, scid)
            wall = safe_float(seg["wall_ms"])
            bubble = safe_float(seg.get("bubble_ratio")) * wall
            tooltip = (
                f"{family_label(family, int(safe_float(seg['main_layer_count'])))}"
                f" · wall {wall:.1f}ms · bubble {fmt_pct(safe_float(seg.get('bubble_ratio')))}"
                f" · 点击进入 L2"
            )
            view_id = f"view-l2-{seg['segment_id']}"
            parts.append(
                f'<rect class="seg" x="{x0:.1f}" y="{row_top+4}" width="{w:.1f}" height="{row_h-8}" '
                f'fill="{color}" rx="2" data-show="{view_id}"><title>{html.escape(tooltip)}</title></rect>'
            )
            if bubble > 0 and wall > 0:
                bubble_w = w * (bubble / wall)
                parts.append(
                    f'<rect x="{x0:.1f}" y="{row_top+4}" width="{bubble_w:.1f}" height="{row_h-8}" '
                    f'fill="url(#bubble-pattern)" rx="2" pointer-events="none"/>'
                )

    families_present = sorted({r.get("step_family", "") for r in b.step_summary})
    legend_items = []
    for f in families_present:
        c = FAMILY_COLOR.get(f, "#58a6ff")
        legend_items.append(
            f'<span style="display:inline-flex;align-items:center;gap:4px;margin-right:14px">'
            f'<span style="display:inline-block;width:12px;height:12px;background:{c};border-radius:2px"></span>'
            f'{html.escape(family_label(f))}</span>'
        )
    legend_items.append(
        '<span style="display:inline-flex;align-items:center;gap:4px;margin-right:14px">'
        '<span style="display:inline-block;width:12px;height:12px;background-image:repeating-linear-gradient(45deg,rgba(248,81,73,.55) 0 2px,transparent 2px 6px);background-color:#1c232c;border-radius:2px"></span>'
        'bubble (idle)</span>'
    )

    return (
        '<div class="card scroll-x">'
        f'<svg class="gantt-svg" width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        '<defs>'
        '<pattern id="bubble-pattern" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">'
        '<rect width="6" height="6" fill="transparent"/>'
        '<line x1="0" y1="0" x2="0" y2="6" stroke="rgba(248,81,73,.55)" stroke-width="2"/>'
        '</pattern></defs>'
        + "".join(parts) +
        '</svg>'
        f'<div style="margin-top:8px;font-size:11px">{"".join(legend_items)}</div>'
        '</div>'
    )


def render_l2_views(b: "Bundle") -> str:
    """Pre-render an L2 view for every step segment."""
    if not b.step_summary:
        return ""
    # rank → ordered steps (so we know the step_index within each rank)
    by_rank = defaultdict(list)
    for s in b.step_summary:
        by_rank[s["rank_id"]].append(s)
    for rid in by_rank:
        by_rank[rid].sort(key=lambda x: safe_float(x["start_us"]))

    # build per-rank step_index map: seg_id → idx
    seg_idx_in_rank: dict[str, int] = {}
    for rid, lst in by_rank.items():
        for i, s in enumerate(lst):
            seg_idx_in_rank[s["segment_id"]] = i

    step_seg_by_id = {s["segment_id"]: s for s in b.step_segments}
    classes_sorted = sorted(b.step_class, key=lambda r: safe_float(r["wall_ms_sum"]), reverse=True)
    top_class_id = classes_sorted[0]["step_class_id"] if classes_sorted else None
    # only top-3 classes have L3 views generated (matches render_l3_views)
    L3_TOP_N = 3
    rep_step_per_class: dict[str, str] = {}
    covered_class_ids: set = set()
    for cls in classes_sorted[:L3_TOP_N]:
        cls_id = cls["step_class_id"]
        members = [s for s in b.step_summary if s.get("step_class_id") == cls_id]
        if members:
            target = safe_float(cls["wall_ms_mean"])
            rep = min(members, key=lambda x: abs(safe_float(x["wall_ms"]) - target))
            rep_step_per_class[cls_id] = rep["segment_id"]
            covered_class_ids.add(cls_id)
    top1_rep_seg = rep_step_per_class.get(top_class_id) if top_class_id else None

    out = []
    for s in b.step_summary:
        seg_id = s["segment_id"]
        view_id = f"view-l2-{seg_id}"
        out.append(_render_l2_single_step(b, s, view_id, seg_idx_in_rank, by_rank,
                                          step_seg_by_id, rep_step_per_class,
                                          covered_class_ids, top1_rep_seg))
    return "".join(out)


def _render_l2_single_step(b: "Bundle", s: dict, view_id: str,
                            seg_idx_in_rank: dict, by_rank: dict,
                            step_seg_by_id: dict,
                            rep_step_per_class: dict,
                            covered_class_ids: set,
                            top1_rep_seg: str | None) -> str:
    seg_id = s["segment_id"]
    rid = s["rank_id"]
    family = s.get("step_family", "")
    scid = s.get("step_class_id", "")
    layer_count = int(safe_float(s.get("main_layer_count", 0)))
    step_idx = seg_idx_in_rank.get(seg_id, 0)
    wall = safe_float(s["wall_ms"])
    bubble_ratio = safe_float(s.get("bubble_ratio", 0))
    start_us = safe_float(s.get("start_us", 0))
    end_us = safe_float(s.get("end_us", 0))

    step_seg_meta = step_seg_by_id.get(seg_id) or {}
    split = split_main_speculative_tail(b, step_seg_meta, rid)

    head_us = split["head_us"]
    main_us = split["main_us"]
    spec_us = split["spec_us"]
    tail_us = split["tail_us"]
    step_busy_us = split["step_busy_us"]
    head_bubble_ms = split["head_bubble_ms"]
    main_bubble_ms = split["main_bubble_ms"]
    tail_bubble_ms = split["tail_bubble_ms"]
    step_wall_ms = split["step_wall_ms"]
    step_wall_us = step_wall_ms * 1000.0
    # Bubble recomputed under the same device+comm-only scope used everywhere else:
    #   bubble_us = step_wall - union(device+comm events in step)
    # If step_busy_us was computed from a partition that doesn't 100% cover, we floor at 0.
    bubble_us = max(0.0, step_wall_us - step_busy_us)
    bubble_pct_recomputed = (bubble_us / step_wall_us) if step_wall_us > 0 else 0

    def pct_of(x):
        return (x / step_wall_us) if step_wall_us > 0 else 0

    # phase split cards
    main_pct = pct_of(main_us)
    spec_pct = pct_of(spec_us)
    tail_pct = pct_of(tail_us)
    bubble_pct = bubble_pct_recomputed
    spec_info = info_btn("speculative_layer") if "speculative_layer" in FIELD_DOC else ""
    spec_layer_count = split["spec_layer_count"]
    phase_split_html = (
        '<div class="phase-split">'
        f'<div class="cell main"><div class="name">主体 (main)</div>'
        f'<div class="val">{main_us/1000:.2f} ms</div>'
        f'<div class="sub">{main_pct*100:.1f}% · main bubble {main_bubble_ms:.2f} ms</div></div>'
        f'<div class="cell spec"><div class="name">投机解码 (spec){spec_info}</div>'
        f'<div class="val">{spec_us/1000:.2f} ms</div>'
        f'<div class="sub">{spec_pct*100:.1f}% · {spec_layer_count} spec layers</div></div>'
        f'<div class="cell tail"><div class="name">尾部小算子+空泡 (tail)</div>'
        f'<div class="val">{tail_us/1000:.2f} ms</div>'
        f'<div class="sub">{tail_pct*100:.1f}% · tail bubble {tail_bubble_ms:.2f} ms</div></div>'
        f'<div class="cell bubble"><div class="name">空泡总计 (bubble)</div>'
        f'<div class="val" style="color:#ff7b72">{bubble_us/1000:.2f} ms</div>'
        f'<div class="sub">{bubble_pct*100:.1f}% · head {head_bubble_ms:.1f} / main {main_bubble_ms:.1f} / tail {tail_bubble_ms:.1f} ms</div></div>'
        '</div>'
    )

    # model guess
    model_guess = guess_model_id(b, s)
    has_attn_v = str(s.get("has_attention", "")).lower() == "true"
    has_moe_v = str(s.get("has_moe", "")).lower() == "true"
    attn_tag = "+attn" if has_attn_v else ""
    moe_tag = "+moe" if has_moe_v else ""
    model_pill = ""
    if model_guess:
        model_pill = (
            f'<div class="model-pill"><span class="lbl">模型反推</span> <b>{html.escape(model_guess)}</b>'
            f'<span class="muted" style="font-size:10px">({layer_count}L · {attn_tag}{moe_tag})</span></div>'
        )

    # cross-rank compare (same step_idx in other ranks)
    xrank_rows = []
    for other_rid in sorted(by_rank.keys()):
        if step_idx >= len(by_rank[other_rid]):
            continue
        other = by_rank[other_rid][step_idx]
        other_wall = safe_float(other["wall_ms"])
        other_bubble = safe_float(other.get("bubble_ratio", 0))
        other_family = other.get("step_family", "")
        diff = (other_wall - wall) / wall * 100 if wall > 0 else 0
        chip = '<span class="badge b-real">本步</span>' if other_rid == rid else ""
        view_link = ""
        if other_rid != rid:
            other_view = f"view-l2-{other['segment_id']}"
            view_link = f'<button class="back-btn" data-show="{other_view}" style="padding:2px 8px">查看</button>'
        diff_color = "#ff7b72" if diff > 5 else ("#79c0ff" if diff < -5 else "var(--muted)")
        xrank_rows.append(
            '<div class="xrank-row">'
            f'<div><b>{html.escape(short_rank_label(other_rid))}</b> {chip}<div class="muted" style="font-size:10px">{html.escape(family_label(other_family))}</div></div>'
            f'<div class="num">{other_wall:.2f} ms</div>'
            f'<div class="num">{other_bubble*100:.1f}%</div>'
            f'<div class="num" style="color:{diff_color}">{diff:+.1f}% vs 本步</div>'
            f'<div>{view_link}</div>'
            '</div>'
        )
    xrank_html = (
        '<div class="card" style="margin-top:14px">'
        '<h3 style="margin-top:0">跨 Rank 同步对比</h3>'
        '<div class="xrank-row head">'
        '<div>Rank</div><div class="num">Wall ms</div><div class="num">Bubble %</div><div class="num">Δ vs 本步</div><div></div>'
        '</div>'
        + "".join(xrank_rows) +
        '</div>'
    )

    # Kernel rollup (top 30) — current step + current rank ONLY.
    # Denominator = step_busy_us (union of active events, no bubble) so layer/kernel
    # ratios actually sum to ~100% across the step.
    rollup = kernel_rollup_by_bound(split["step_events"])
    # Recompute each kernel's union-time (not sum) so concurrent AIC+AIV don't double count
    name_to_union = union_duration_us_by_name(split["step_events"])
    max_dur = rollup[0]["duration_us"] if rollup else 1.0
    krows = ['<div class="kernel-row head" style="grid-template-columns:1.5fr 0.5fr 0.4fr 1.6fr 0.5fr 0.7fr">'
             '<div>Kernel</div><div>Op type</div><div class="num">Calls</div>'
             '<div>Σ (in this step) · % of step active</div>'
             '<div>Bound family</div><div>Bound stage</div>'
             '</div>']
    for r in rollup[:30]:
        op_type = r["op_type"]
        color = OP_TYPE_COLOR.get(op_type, "#8b949e")
        bf = r["bound_family"]
        bf_color = BOUND_FAMILY_COLOR.get(bf, "#8b949e")
        # union-based share against step_busy
        union_us = name_to_union.get(r["kernel"], r["duration_us"])
        dur_pct_step = (union_us / step_busy_us * 100) if step_busy_us > 0 else 0
        bar_pct = (union_us / step_busy_us * 100) if step_busy_us > 0 else 0
        krows.append(
            '<div class="kernel-row" style="grid-template-columns:1.5fr 0.5fr 0.4fr 1.6fr 0.5fr 0.7fr">'
            f'<div class="name" title="{html.escape(r["kernel"])}">{html.escape(r["kernel"])}</div>'
            f'<div><span class="badge" style="background:{color}33;color:{color}">{html.escape(op_type)}</span></div>'
            f'<div class="num">{r["count"]}</div>'
            '<div class="bar-host" title="union of all calls of this kernel in this step">'
            f'<div class="bar-fill" style="width:{min(bar_pct, 100):.1f}%;background:{color}"></div>'
            f'<div class="bar-lbl">{union_us/1000:.2f} ms · {dur_pct_step:.1f}%</div>'
            '</div>'
            f'<div><span class="badge" style="background:{bf_color}33;color:{bf_color}">{html.escape(bf)}</span></div>'
            f'<div class="muted" style="font-size:10.5px">{html.escape(r["bound_stage"])}</div>'
            '</div>'
        )
    rollup_extra = (
        f'<div class="muted" style="font-size:11px;margin-top:6px">'
        f'分母 = 本 step 在本 rank 上所有 device 事件 (AIV/AIC/mix_cv/mix_comm_aiv/communication/aicpu, '
        f'去 redundant) 的 active union = <b>{step_busy_us/1000:.2f} ms</b>'
        f'（step wall = {step_wall_ms:.2f} ms，差额 = bubble）'
        + (f' · 仅显示前 30 / 共 {len(rollup)} 种 kernel' if len(rollup) > 30 else '')
        + '</div>'
    )

    # Layer list — every layer routes to its step_class's rep step's L3 view.
    layers_in_step = sorted([
        ls for ls in b.layer_segments
        if ls["rank_id"] == rid
        and ls["row_start"] >= step_seg_meta.get("row_start", 0)
        and ls["row_end"] <= step_seg_meta.get("row_end", 0)
    ], key=lambda x: x["row_start"])

    # find the L3 target step for this step's step_class.
    # Precedence: 1) this step IS the rep → use own L3
    #             2) this step's class is in top-N → use class's rep L3
    #             3) fallback → use top-1 class's rep L3 (best-effort same layer_index)
    own_cls = s.get("step_class_id", "")
    own_rep_seg = rep_step_per_class.get(own_cls)
    is_rep_self = (own_rep_seg == seg_id)
    own_class_covered = own_cls in covered_class_ids

    if is_rep_self:
        target_seg = seg_id
        target_kind = "self"  # this step IS the rep
    elif own_class_covered and own_rep_seg:
        target_seg = own_rep_seg
        target_kind = "class_rep"
    elif top1_rep_seg:
        target_seg = top1_rep_seg
        target_kind = "top1_fallback"
    else:
        target_seg = None
        target_kind = "none"

    layer_rows_html = []
    for ls in layers_in_step:
        lev = events_in_row_range(b.events, ls["row_start"], ls["row_end"], rid)
        lev_active = [e for e in lev if not getattr(e, "redundant", False)]
        # Use union, not sum, so AIC + AIV concurrent activity isn't double-counted.
        ldur = union_duration_us(lev_active)
        lay_idx = ls.get("layer_index", "?")
        role = ls.get("layer_role", "main")
        composition = derive_layer_composition(b, ls)
        layer_step_pct = (ldur / step_busy_us * 100) if step_busy_us > 0 else 0
        clickable = target_seg is not None
        view_l3 = f"view-l3-{target_seg}-{lay_idx}-{role}" if clickable else ""
        if target_kind == "self":
            cross_hint = ""
        elif target_kind == "class_rep":
            cross_hint = ' <span class="muted" style="font-size:10px">(on class rep)</span>'
        elif target_kind == "top1_fallback":
            cross_hint = ' <span class="muted" style="font-size:10px">(on top-1 rep)</span>'
        else:
            cross_hint = ' <span class="muted" style="font-size:10px">(no L3 available)</span>'
        click_attr = f'data-show="{view_l3}"' if clickable else 'style="cursor:default;opacity:.55"'
        cursor_style = ";cursor:pointer" if clickable else ""
        # mini bar visualizing % of step
        pct_color = "#3fb950" if layer_step_pct > 5 else ("#f0883e" if layer_step_pct > 2 else "#58a6ff")
        layer_rows_html.append(
            '<div class="kernel-row" '
            f'style="grid-template-columns:50px 1.2fr 0.6fr 1.0fr 0.5fr 0.4fr 0.4fr{cursor_style}" '
            f'{click_attr}>'
            f'<div><span class="muted">L{lay_idx}</span></div>'
            f'<div><b style="color:#79c0ff">{html.escape(composition)}</b>{cross_hint}</div>'
            f'<div class="num">{ldur/1000:.2f} ms</div>'
            '<div class="bar-host" style="position:relative;height:14px;background:#0d1117;border-radius:2px;border:1px solid #1c232c">'
            f'<div class="bar-fill" style="position:absolute;left:0;top:0;height:100%;width:{min(layer_step_pct, 100):.1f}%;background:{pct_color};opacity:.6;border-radius:2px"></div>'
            f'<div style="position:relative;line-height:14px;padding:0 6px;font-variant-numeric:tabular-nums;font-size:10.5px">{layer_step_pct:.2f}%</div>'
            '</div>'
            f'<div class="num muted">{len(lev)} events</div>'
            f'<div><span class="chip" style="font-size:10px">{html.escape(role)}</span></div>'
            f'<div class="muted" style="font-size:10.5px;text-align:right">{"→ L3" if clickable else "—"}</div>'
            '</div>'
        )
    rep_note = ""
    if target_kind == "class_rep":
        rep_note = (
            '<div class="muted" style="font-size:11px;margin-top:6px">'
            '本 step 与其 step_class 的代表 step 结构一致；点击 layer 跳到代表 step 的 L3 详情。'
            '</div>'
        )
    elif target_kind == "top1_fallback":
        rep_note = (
            '<div class="muted" style="font-size:11px;margin-top:6px">'
            '本 step 的 step_class 未在 top-3 内（L3 仅生成 top-3 by wall_ms_sum）；点击 layer 跳到 top-1 代表 step 的同 layer_index，作为最接近的结构参考。'
            '</div>'
        )
    elif target_kind == "none":
        rep_note = (
            '<div class="muted" style="font-size:11px;margin-top:6px">'
            '未找到任何 L3 目标。'
            '</div>'
        )
    layers_block = (
        '<div class="card" style="margin-top:14px">'
        '<h3 style="margin-top:0">Layer 顺序 · 点击任一 layer 进入 L3</h3>'
        '<div class="muted" style="font-size:11.5px;margin-bottom:6px">'
        '<b>Composition</b> 列：根据 block_segments 推断的 attention sub-type + ffn/moe 组合。'
        '<code>gqa</code> = FIA 路径（vLLM-Ascend Qwen/Llama）；<code>mla</code> = DeepSeek 系列稀疏 attention（SparseAttnSharedkv / Compressor / MatmulCompressedKV）；'
        '<code>fa</code> = FlashAttention prefill 路径。'
        '</div>'
        '<div class="kernel-rollup" style="margin-top:6px">'
        '<div class="kernel-row head" style="grid-template-columns:50px 1.2fr 0.6fr 1.0fr 0.5fr 0.4fr 0.4fr">'
        '<div>idx</div><div>Composition</div><div class="num">Active ms</div><div class="num">% of step active</div><div class="num">Events</div><div>Role</div><div></div>'
        '</div>'
        + "".join(layer_rows_html) +
        '</div>'
        + f'<div class="muted" style="font-size:11px;margin-top:6px">'
        f'分母 = 本 step 在本 rank 上所有 device 事件 (AIV/AIC/mix/comm/aicpu, 去 redundant) 的 active union = <b>{step_busy_us/1000:.2f} ms</b>；'
        f'分子 = 本 layer 在本 rank 上同口径的 active union（跨流取并集，AIC/AIV 同时活跃不双计；redundant 标记的 AIV 与 HCCL 双流副本不双计）'
        + '</div>'
        + rep_note
        + '</div>'
    )

    fam_lbl = family_label(family, layer_count) if family else (f"Step #{step_idx + 1}")
    title = f"{fam_lbl} · rank {short_rank_label(rid)} · step #{step_idx + 1}"
    head = (
        '<div class="card">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">'
        f'<div><h1 style="margin:0">{html.escape(title)}</h1>'
        f'<div class="muted" style="font-size:11.5px">'
        f'wall <b>{wall:.2f}</b> ms · bubble <b style="color:#ff7b72">{bubble_ratio*100:.1f}%</b> · '
        f'time window <code>{start_us/1000:.2f} → {end_us/1000:.2f} ms</code> · segment_id <code>{html.escape(seg_id)}</code>'
        '</div></div>'
        f'{model_pill}'
        '</div>'
        '</div>'
    )

    return (
        f'<section class="view" id="{view_id}" data-level="2" data-l2id="{view_id}" '
        f'data-l2title="{html.escape(title)}" data-title="{html.escape(title)}">'
        f'{head}'
        f'<div class="card" style="margin-top:14px"><h3 style="margin-top:0">阶段分区</h3>'
        f'{phase_split_html}'
        '<div class="muted" style="font-size:11px;margin-top:6px">'
        '主体 = main layer 内事件 · 投机 = layer_role=spec 内事件 · 尾部 = tail 段事件 · 空泡比例来自 step_anatomy'
        '</div></div>'
        f'{xrank_html}'
        '<div class="card" style="margin-top:14px">'
        '<h3 style="margin-top:0">Kernel 占比 · 按耗时降序</h3>'
        '<div class="muted" style="font-size:11.5px;margin-bottom:6px">所有 kernel 按本 step 内的总耗时降序排列；bound family 是该 kernel 的 ratio-加权主要瓶颈面</div>'
        '<div class="kernel-rollup">'
        + "".join(krows) +
        '</div>'
        + rollup_extra +
        '</div>'
        f'{layers_block}'
        '</section>'
    )


def render_l3_views(b: "Bundle") -> str:
    """Pre-render L3 view for every layer of every step-class's representative step.

    L3 = ordered list of operators in the layer; click a row to expand its op-card.
    """
    if not b.step_class:
        return ""
    # generate L3 views for the top-N step classes by wall_ms_sum. Uncovered classes'
    # layer clicks fall back to the top-1 class's rep step (best-effort same-layer-index).
    rep_seg_ids: list[str] = []
    classes_sorted = sorted(b.step_class, key=lambda r: safe_float(r["wall_ms_sum"]), reverse=True)
    L3_TOP_N = 3
    for cls in classes_sorted[:L3_TOP_N]:
        cls_id = cls["step_class_id"]
        members = [s for s in b.step_summary if s.get("step_class_id") == cls_id]
        if not members:
            continue
        target = safe_float(cls["wall_ms_mean"])
        rep = min(members, key=lambda x: abs(safe_float(x["wall_ms"]) - target))
        rep_seg_ids.append(rep["segment_id"])

    step_seg_by_id = {s["segment_id"]: s for s in b.step_segments}
    out = []

    for seg_id in rep_seg_ids:
        step_meta = step_seg_by_id.get(seg_id)
        if not step_meta:
            continue
        rank_id = next((s["rank_id"] for s in b.step_summary if s["segment_id"] == seg_id), None)
        if rank_id is None:
            continue
        # Precompute step-scope context once per step (kernel-step union + step_busy).
        step_events = events_in_row_range(b.events, step_meta["row_start"], step_meta["row_end"], rank_id)
        step_events_active = [e for e in step_events if not getattr(e, "redundant", False)]
        step_busy_us = union_duration_us(step_events_active)
        kernel_step_union_us = union_duration_us_by_name(step_events_active)
        kernel_step_count = Counter(short_op_name(e.name) for e in step_events_active)

        layers = sorted([
            ls for ls in b.layer_segments
            if ls["rank_id"] == rank_id
            and ls["row_start"] >= step_meta["row_start"]
            and ls["row_end"] <= step_meta["row_end"]
        ], key=lambda x: x["row_start"])
        for ls in layers:
            lay_idx = ls.get("layer_index", "?")
            role = ls.get("layer_role", "main")
            view_id = f"view-l3-{seg_id}-{lay_idx}-{role}"
            out.append(_render_l3_layer(
                b, view_id, seg_id, ls, rank_id,
                step_busy_us=step_busy_us,
                kernel_step_union_us=kernel_step_union_us,
                kernel_step_count=kernel_step_count,
            ))
    return "".join(out)


def _render_l3_layer(b: "Bundle", view_id: str, parent_seg_id: str,
                     ls: dict, rank_id: str,
                     step_busy_us: float,
                     kernel_step_union_us: dict,
                     kernel_step_count: Counter) -> str:
    lay_idx = ls.get("layer_index", "?")
    role = ls.get("layer_role", "main")
    lev = events_in_row_range(b.events, ls["row_start"], ls["row_end"], rank_id)
    lev_active = [e for e in lev if not getattr(e, "redundant", False)]
    lev_active.sort(key=lambda e: e.start_us)
    # Use union, not sum — AIC + AIV running concurrently must not be double-counted.
    layer_busy_us = union_duration_us(lev_active)
    kernel_layer_union_us = union_duration_us_by_name(lev_active)
    kernel_layer_count = Counter(short_op_name(e.name) for e in lev_active)

    title = f"Layer {lay_idx} · {role} · active {layer_busy_us/1000:.2f} ms · {len(lev_active)} ops"
    l2_view = f"view-l2-{parent_seg_id}"

    list_html = ['<div class="op-list-row head">'
                 '<div class="ix">#</div>'
                 '<div class="nm">Operator</div>'
                 '<div>Op type</div>'
                 '<div class="num">Stream</div>'
                 '<div class="num">Duration μs</div>'
                 '<div class="num">% of layer</div>'
                 '<div>Bound</div>'
                 '<div></div>'
                 '</div>']
    for i, e in enumerate(lev_active):
        op_type = e.op_type
        color = OP_TYPE_COLOR.get(op_type, "#8b949e")
        bound = pick_bound_stage(e.pipeline) if e.pipeline else None
        bf = STAGE_FAMILY.get(bound, "unknown") if bound else "unknown"
        bf_color = BOUND_FAMILY_COLOR.get(bf, "#8b949e")
        pct = (e.duration_us / layer_busy_us * 100) if layer_busy_us > 0 else 0
        stream = e.stream_id or "—"
        card_id = f"opcard-host-{view_id}-{i}"
        list_html.append(
            f'<div class="op-list-row" data-card-id="{card_id}">'
            f'<div class="ix">{i+1}</div>'
            f'<div class="nm" title="{html.escape(e.name)}">{html.escape(short_op_name(e.name))}</div>'
            f'<div><span class="badge" style="background:{color}33;color:{color}">{html.escape(op_type)}</span></div>'
            f'<div class="num muted" style="font-family:\'SF Mono\',Menlo,Consolas,monospace;font-size:10.5px">{html.escape(str(stream))}</div>'
            f'<div class="num">{e.duration_us:.2f}</div>'
            f'<div class="num">{pct:.2f}%</div>'
            f'<div><span class="badge" style="background:{bf_color}33;color:{bf_color}">{html.escape(bf)}</span></div>'
            '<div style="text-align:right;color:var(--muted);font-size:14px">▾</div>'
            '</div>'
        )
        list_html.append(
            f'<div class="op-card-host hidden" id="{card_id}">'
            + render_operator_card(
                e, layer_busy_us,
                card_id=f"opcard-{view_id}-{i}",
                step_busy_us=step_busy_us,
                kernel_layer_union_us=kernel_layer_union_us,
                kernel_layer_count=kernel_layer_count,
                kernel_step_union_us=kernel_step_union_us,
                kernel_step_count=kernel_step_count,
            )
            + '</div>'
        )

    return (
        f'<section class="view" id="{view_id}" data-level="3" data-l2id="{l2_view}" '
        f'data-title="{html.escape(title)}">'
        '<div class="card">'
        f'<h2 style="margin:0">{html.escape(title)}</h2>'
        '<div class="muted" style="font-size:11.5px">'
        '按执行顺序排列；点击任一算子展开 46 字段算子卡 / pipeline ratio / IR 签名'
        '</div>'
        '</div>'
        '<div class="card" style="margin-top:12px">'
        '<div class="op-list">'
        + "".join(list_html) +
        '</div>'
        + '</div>'
        '</section>'
    )


def build_html_report(analysis_root: Path | str, output_path: Path | str) -> Path:
    """Render the v7 SPA HTML report for the given analysis root.

    Three-level focus: L1 总览 · L2 单步 · L3 局部。
    """
    root = Path(analysis_root)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    b = load_bundle(root)
    title = f"Ascend Profiling · {os.path.basename(str(root).rstrip('/'))}"
    html_out = "".join([
        render_head(title),
        render_l1_view(b),
        render_l2_views(b),
        render_l3_views(b),
        render_foot(),
    ])
    output.write_text(html_out, encoding="utf-8")
    return output.resolve()


def main():
    if len(sys.argv) < 3:
        print(f"usage: {sys.argv[0]} <profile_analysis_root> <output.html>", file=sys.stderr)
        sys.exit(2)
    root = Path(sys.argv[1])
    output = Path(sys.argv[2])
    path = build_html_report(root, output)
    size_kb = output.stat().st_size / 1024
    print(f"wrote {path} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
