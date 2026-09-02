#!/usr/bin/env python3
"""Profiling-derived model and operator-work insight tables.

This module folds two useful reference ideas into the profiling skill:

* LLMInsight-style per-operator FLOPs / bytes / arithmetic-intensity /
  roofline headroom estimates from CANN shape fields.
* model_analysis-style model structure summaries, but with profiler rows as
  the primary source of truth.  ``config.json`` can be used as an optional
  comparison source, never as a prerequisite.

Every number here is a derived estimate.  The report keeps these tables
separate from diagnosis findings so they can guide investigation without
weakening the row-level evidence contract.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .common import NormalizedEvent, dtype_bytes, quantile
    from .hardware_insights import memory_bandwidth_bytes_per_second, peak_flops_per_second
    from .model_context import MODEL_FINGERPRINTS_PATH, load_model_fingerprints
    from .store import first_present, text_config, to_float, to_int
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import NormalizedEvent, dtype_bytes, quantile  # type: ignore[no-redef]
    from hardware_insights import memory_bandwidth_bytes_per_second, peak_flops_per_second  # type: ignore[no-redef]
    from model_context import MODEL_FINGERPRINTS_PATH, load_model_fingerprints  # type: ignore[no-redef]
    from store import first_present, text_config, to_float, to_int  # type: ignore[no-redef]


def _safe_i(value: Any) -> int | None:
    try:
        if value is None or value == "" or value == "unknown":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _fmt_params(value: float) -> str:
    if value >= 1e12:
        return f"{value / 1e12:.3f}T"
    if value >= 1e9:
        return f"{value / 1e9:.3f}B"
    if value >= 1e6:
        return f"{value / 1e6:.3f}M"
    return f"{value:.0f}"


def _layer_kind_counts(cfg: Mapping[str, Any], n_layers: int) -> Counter[str]:
    layer_types = cfg.get("layer_types")
    if isinstance(layer_types, list) and layer_types:
        return Counter(str(item) for item in layer_types)
    if to_int(first_present(cfg, "full_attention_interval", default=0)) > 0 and n_layers > 0:
        interval = to_int(cfg.get("full_attention_interval"))
        full = n_layers // interval
        return Counter({"linear_attention": max(n_layers - full, 0), "full_attention": full})
    return Counter({"default": n_layers})


def _attention_features(root: Mapping[str, Any], cfg: Mapping[str, Any]) -> list[str]:
    features: list[str] = []
    layer_types = cfg.get("layer_types") if isinstance(cfg.get("layer_types"), list) else []
    if to_int(first_present(cfg, "q_lora_rank", "kv_lora_rank", default=0)) > 0:
        features.append("mla")
    if any("linear" in str(item).lower() for item in layer_types) or to_int(first_present(cfg, "linear_attention_dim", "linear_key_head_dim", default=0)) > 0:
        features.append("linear_attention")
    if any("full" in str(item).lower() for item in layer_types) or to_int(first_present(cfg, "num_key_value_heads", "n_kv_heads", default=0)) > 0:
        features.append("gqa_or_mha")
    if to_int(first_present(cfg, "index_topk", default=0)) > 0 or to_int(first_present(cfg, "index_n_heads", default=0)) > 0:
        features.append("dsa_indexer")
    if isinstance(cfg.get("compress_ratios"), list):
        features.append("kv_compressor")
    rope = cfg.get("rope_parameters")
    if isinstance(rope, Mapping) and rope.get("mrope_section"):
        features.append("mrope")
    if isinstance(root.get("vision_config"), Mapping):
        features.append("vision")
    return list(dict.fromkeys(features)) or ["unknown"]


def _parameter_rows(root: Mapping[str, Any], cfg: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, float]]:
    hidden = to_int(first_present(cfg, "hidden_size", "dim", default=0))
    vocab = to_int(first_present(cfg, "vocab_size", default=0))
    layers = to_int(first_present(cfg, "num_hidden_layers", "n_layers", default=0))
    heads = to_int(first_present(cfg, "num_attention_heads", "n_heads", default=0))
    kv_heads = to_int(first_present(cfg, "num_key_value_heads", "n_kv_heads", default=heads))
    head_dim = to_int(first_present(cfg, "head_dim", default=(hidden // heads if heads else 0)))
    q_lora = to_int(first_present(cfg, "q_lora_rank", default=0))
    kv_lora = to_int(first_present(cfg, "kv_lora_rank", default=0))
    qk_rope = to_int(first_present(cfg, "qk_rope_head_dim", "rope_head_dim", default=0))
    qk_nope = to_int(first_present(cfg, "qk_nope_head_dim", default=max(head_dim - qk_rope, 0)))
    v_head = to_int(first_present(cfg, "v_head_dim", default=head_dim))
    intermediate = to_int(first_present(cfg, "intermediate_size", "ffn_dim", default=0))
    moe_inter = to_int(first_present(cfg, "moe_intermediate_size", "moe_inter_dim", default=intermediate))
    experts = to_int(first_present(cfg, "num_experts", "n_routed_experts", default=0))
    top_k = to_int(first_present(cfg, "num_experts_per_tok", "n_activated_experts", default=0))
    shared_inter = to_int(first_present(cfg, "shared_expert_intermediate_size", default=0))
    first_dense_layers = max(to_int(first_present(cfg, "first_k_dense_replace", default=0)), 0)
    raw_moe_frequency = first_present(cfg, "moe_layer_freq", default=None)
    raw_decoder_sparse_step = first_present(cfg, "decoder_sparse_step", default=None)
    moe_frequency = max(to_int(raw_moe_frequency, 1), 1)
    decoder_sparse_step = max(to_int(raw_decoder_sparse_step, 1), 1)
    raw_mlp_only_layers = cfg.get("mlp_only_layers")
    mlp_only_layers = (
        {to_int(layer_idx, -1) for layer_idx in raw_mlp_only_layers}
        if isinstance(raw_mlp_only_layers, list)
        else set()
    )

    rows: list[dict[str, Any]] = []

    def add(component: str, params: float, active: float | None, formula: str) -> None:
        rows.append(
            {
                "component": component,
                "params": round(params, 3),
                "params_human": _fmt_params(params),
                "active_params": round(active if active is not None else params, 3),
                "active_params_human": _fmt_params(active if active is not None else params),
                "formula": formula,
                "confidence": "derived_from_config",
            }
        )

    embedding = vocab * hidden if vocab and hidden else 0
    if embedding:
        add("embedding", embedding, embedding, "vocab_size * hidden_size")

    if q_lora or kv_lora:
        if q_lora:
            q_path = hidden * q_lora + q_lora * heads * (qk_nope + qk_rope)
            q_formula = "q_a + q_b"
        else:
            # MLA variants without Q LoRA retain a full Q projection. Treating
            # q_lora_rank=0 as a zero-sized low-rank path omits the entire Q
            # matrix from the parameter estimate.
            q_path = hidden * heads * (qk_nope + qk_rope)
            q_formula = "full_q_proj"
        kv_path = hidden * (kv_lora + qk_rope) + kv_lora * heads * (qk_nope + v_head)
        o_proj = heads * v_head * hidden
        attn = layers * (q_path + kv_path + o_proj)
        add("attention_mla", attn, attn, f"layers * ({q_formula} + kv_a + kv_b + o_proj)")
    elif hidden and heads and head_dim:
        q_proj = hidden * heads * head_dim
        kv_proj = 2 * hidden * kv_heads * head_dim
        o_proj = heads * head_dim * hidden
        attn = layers * (q_proj + kv_proj + o_proj)
        add("attention_gqa_or_mha", attn, attn, "layers * (q_proj + k_proj + v_proj + o_proj)")

    if experts and moe_inter:
        def is_moe_layer(layer_idx: int) -> bool:
            if layer_idx in mlp_only_layers:
                return False
            if raw_moe_frequency is not None:
                # DeepSeek/Kimi/AXK-style convention.
                return layer_idx >= first_dense_layers and layer_idx % moe_frequency == 0
            if raw_decoder_sparse_step is not None:
                # Qwen-style convention counts layers from one.
                return (layer_idx + 1) % decoder_sparse_step == 0
            return layer_idx >= first_dense_layers

        moe_layer_count = sum(
            1
            for layer_idx in range(layers)
            if is_moe_layer(layer_idx)
        )
        dense_layer_count = max(layers - moe_layer_count, 0)
        expert_params_per_layer = experts * 3 * hidden * moe_inter
        active_expert_params = max(top_k, 1) * 3 * hidden * moe_inter
        gate = hidden * experts
        shared = 3 * hidden * shared_inter if shared_inter else 0
        if moe_layer_count:
            add(
                "moe_routed_experts",
                moe_layer_count * expert_params_per_layer,
                moe_layer_count * active_expert_params,
                "moe_layers * experts * 3 * hidden * moe_intermediate",
            )
            if shared:
                add(
                    "moe_shared_expert",
                    moe_layer_count * shared,
                    moe_layer_count * shared,
                    "moe_layers * 3 * hidden * shared_expert_intermediate",
                )
            add(
                "moe_router",
                moe_layer_count * gate,
                moe_layer_count * gate,
                "moe_layers * hidden * experts",
            )
        if dense_layer_count and intermediate:
            dense = dense_layer_count * 3 * hidden * intermediate
            add(
                "dense_swiglu_ffn",
                dense,
                dense,
                "dense_layers * 3 * hidden * intermediate_size",
            )
    elif intermediate:
        dense = layers * 3 * hidden * intermediate
        add("dense_swiglu_ffn", dense, dense, "layers * 3 * hidden * intermediate_size")

    total = sum(float(row["params"]) for row in rows)
    active_total = sum(float(row["active_params"]) for row in rows)
    tied = bool(root.get("tie_word_embeddings"))
    if embedding and not tied:
        add("lm_head_untied", embedding, embedding, "vocab_size * hidden_size (tie_word_embeddings=false)")
        total += embedding
        active_total += embedding
    return rows, {"total_params": total, "active_params": active_total}


def _kv_rows(cfg: Mapping[str, Any], features: Sequence[str]) -> list[dict[str, Any]]:
    hidden = to_int(first_present(cfg, "hidden_size", "dim", default=0))
    heads = to_int(first_present(cfg, "num_attention_heads", "n_heads", default=0))
    kv_heads = to_int(first_present(cfg, "num_key_value_heads", "n_kv_heads", default=heads))
    head_dim = to_int(first_present(cfg, "head_dim", default=(hidden // heads if heads else 0)))
    kv_lora = to_int(first_present(cfg, "kv_lora_rank", default=0))
    qk_rope = to_int(first_present(cfg, "qk_rope_head_dim", "rope_head_dim", default=0))
    index_heads = to_int(first_present(cfg, "index_n_heads", default=0))
    index_dim = to_int(first_present(cfg, "index_head_dim", default=0))
    dtype = str(first_present(cfg, "dtype", "torch_dtype", default="bfloat16"))
    dt_bytes = dtype_bytes(dtype)
    rows: list[dict[str, Any]] = []
    full = kv_heads * head_dim * 2 * dt_bytes if kv_heads and head_dim else 0.0
    if full:
        rows.append(
            {
                "cache_type": "full_gqa_or_mha",
                "bytes_per_token_per_layer": round(full, 3),
                "state_bytes_per_batch": 0,
                "compression_vs_full": 1.0,
                "formula": "num_key_value_heads * head_dim * 2 * dtype_bytes",
            }
        )
    if kv_lora or "mla" in features:
        mla = (kv_lora + qk_rope) * dt_bytes
        rows.append(
            {
                "cache_type": "mla_absorption",
                "bytes_per_token_per_layer": round(mla, 3),
                "state_bytes_per_batch": 0,
                "compression_vs_full": round(full / mla, 6) if mla and full else None,
                "formula": "(kv_lora_rank + qk_rope_head_dim) * dtype_bytes",
            }
        )
    if "linear_attention" in features:
        lin_heads = to_int(first_present(cfg, "linear_num_key_heads", "num_attention_heads", "n_heads", default=heads))
        lin_key = to_int(first_present(cfg, "linear_key_head_dim", "head_dim", default=head_dim))
        lin_val = to_int(first_present(cfg, "linear_value_head_dim", "head_dim", default=head_dim))
        state = lin_heads * lin_key * lin_val * dt_bytes
        rows.append(
            {
                "cache_type": "linear_attention_recurrent_state",
                "bytes_per_token_per_layer": 0,
                "state_bytes_per_batch": round(state, 3),
                "compression_vs_full": None,
                "formula": "linear_heads * linear_key_dim * linear_value_dim * dtype_bytes",
            }
        )
    if index_heads and index_dim:
        index_bytes = index_heads * index_dim * dt_bytes
        rows.append(
            {
                "cache_type": "dsa_indexer_key",
                "bytes_per_token_per_layer": round(index_bytes, 3),
                "state_bytes_per_batch": 0,
                "compression_vs_full": round(full / index_bytes, 6) if index_bytes and full else None,
                "formula": "index_n_heads * index_head_dim * dtype_bytes",
            }
        )
    ratios = cfg.get("compress_ratios")
    if isinstance(ratios, list) and ratios and head_dim:
        nonzero = [float(item) for item in ratios if to_float(item) > 0]
        if nonzero:
            min_ratio = min(nonzero)
            max_ratio = max(nonzero)
            rows.append(
                {
                    "cache_type": "kv_compressor",
                    "bytes_per_token_per_layer": round(2 * head_dim * dt_bytes / min_ratio, 3),
                    "state_bytes_per_batch": 0,
                    "compression_vs_full": round(full / (2 * head_dim * dt_bytes / min_ratio), 6) if full else None,
                    "formula": f"2 * head_dim * dtype_bytes / compress_ratio; ratio range {min_ratio:g}-{max_ratio:g}",
                }
            )
    return rows


def _feature_rows(root: Mapping[str, Any], cfg: Mapping[str, Any], features: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(name: str, priority: str, reason: str, evidence: str) -> None:
        rows.append({"feature": name, "priority": priority, "reason": reason, "config_evidence": evidence})

    if to_int(first_present(cfg, "num_experts", "n_routed_experts", default=0)) > 0:
        add("FusedMoE / grouped expert matmul", "P1", "MoE dispatch, routed expert GEMM, and combine dominate token-parallel inference paths.", "num_experts/n_routed_experts")
    if "mla" in features:
        add("FusedMLAProj", "P1", "MLA splits Q/KV into low-rank projections; fusing projection and cache preparation reduces launch and memory traffic.", "q_lora_rank/kv_lora_rank")
        add("MLA absorption cache path", "P1", "KV cache can be stored in latent space; runtime needs an absorption-aware attention/cache manager.", "kv_lora_rank + qk_rope_head_dim")
    if "linear_attention" in features:
        add("FusedGatedDeltaRule / FusedSimpleGLA", "P0", "Linear attention uses recurrent-state update kernels rather than dense KV attention.", "layer_types contains linear_attention")
        if to_int(first_present(cfg, "linear_conv_kernel_dim", default=0)) > 0:
            add("FusedCausalConv1d", "P1", "Linear-attention front-end includes causal convolution over token states.", "linear_conv_kernel_dim")
    if "dsa_indexer" in features:
        add("FusedDSAIndexer", "P0", "Sparse attention needs top-k index scoring and gather-friendly metadata generation.", "index_topk/index_n_heads")
    if "kv_compressor" in features:
        add("KV compressor kernels", "P1", "Compressed attention needs per-layer compression ratios and cache write/read kernels.", "compress_ratios")
    if len({"linear_attention", "gqa_or_mha", "mla"} & set(features)) > 1:
        add("Hybrid cache manager", "P1", "Layer mix implies multiple cache layouts in one model.", "layer_types/features")
    if "mrope" in features:
        add("MRoPE", "P1", "Multimodal rotary sections require 3D position handling in attention prep.", "rope_parameters.mrope_section")
    if isinstance(root.get("vision_config"), Mapping):
        add("Vision encoder integration", "P2", "Multimodal config carries a vision tower whose prefill path should be analyzed separately.", "vision_config")
    return rows


def _shape_samples(event: NormalizedEvent) -> tuple[list[list[int]], list[list[int]]]:
    ins = event.shape_features.get("input_shape_sample") or []
    outs = event.shape_features.get("output_shape_sample") or []
    return (
        [list(shape) for shape in ins if isinstance(shape, list)],
        [list(shape) for shape in outs if isinstance(shape, list)],
    )


def _add_candidate(counter: Counter[int], value: Any, *, min_value: int = 1, max_value: int = 1_000_000) -> None:
    intval = to_int(value)
    if min_value <= intval <= max_value:
        counter[intval] += 1


def _shape_numel(shape: Sequence[int]) -> int:
    total = 1
    for dim in shape:
        if dim <= 0:
            return 0
        total *= int(dim)
    return total


def _top_candidates(counter: Counter[int], limit: int = 5) -> list[dict[str, Any]]:
    total = sum(counter.values())
    rows: list[dict[str, Any]] = []
    for value, count in counter.most_common(limit):
        rows.append(
            {
                "value": value,
                "observations": count,
                "share": round(count / total, 6) if total else 0.0,
            }
        )
    return rows


def _matmul_weight_shape(ins: Sequence[Sequence[int]]) -> list[int]:
    if len(ins) < 2:
        return []
    second = list(ins[1])
    if len(second) < 2:
        return []
    return second


def _lm_head_name_hint(event: NormalizedEvent) -> bool:
    hay = f"{event.name_raw} {event.task_type}".upper()
    hints = ("LM_HEAD", "LMHEAD", "LOGITS", "VOCAB", "OUTPUT_PROJECTION", "OUTPUTPROJECTION")
    return any(item in hay for item in hints)


def _infer_vocab_from_lm_head(events: Sequence[NormalizedEvent]) -> dict[str, Any]:
    """Infer vocab or vocab-shard candidates from lm_head/logits matmul shapes.

    A distributed lm_head may expose only the TP-sharded output dimension.  The
    row therefore deliberately reports ``vocab_size_or_lm_head_shard`` instead
    of claiming global ``vocab_size``.
    """

    strong: Counter[int] = Counter()
    weak: Counter[int] = Counter()
    evidence: list[str] = []
    for event in events:
        work = str(event.shape_features.get("estimated_work_class") or "")
        cats = set(event.op_categories)
        if work != "matmul" and "compute.matmul" not in cats:
            continue
        ins, outs = _shape_samples(event)
        dims: list[int] = []
        weight = _matmul_weight_shape(ins)
        if len(weight) >= 2:
            dims.extend([weight[-2], weight[-1]])
        for out in outs:
            if len(out) >= 2:
                dims.append(out[-1])
        is_hint = _lm_head_name_hint(event)
        for dim in dims:
            # Most modern LLM vocabularies are well above hidden/intermediate
            # head dims.  This still remains a candidate, not a proof.
            if 16_000 <= int(dim) <= 1_000_000:
                (strong if is_hint else weak)[int(dim)] += 1
                if len(evidence) < 16:
                    evidence.append(event.event_id)
    return {
        "lm_head_name_hint_candidates": _top_candidates(strong),
        "large_output_dim_candidates": _top_candidates(weak),
        "sample_event_ids": evidence,
    }


def _rank_visible_matmul_parameter_estimate(events: Sequence[NormalizedEvent]) -> dict[str, Any]:
    """Estimate rank-visible weight elements from matmul weight shapes.

    We count distinct ``(name, task, weight_shape)`` signatures once.  This is
    intentionally a lower-bound/visibility estimate because profiler rows do
    not prove whether repeated same-shaped kernels correspond to distinct layer
    weights, reused graph nodes, or TP/EP shards.
    """

    signatures: set[tuple[str, str, tuple[int, ...]]] = set()
    examples: list[dict[str, Any]] = []
    for event in events:
        work = str(event.shape_features.get("estimated_work_class") or "")
        cats = set(event.op_categories)
        if work != "matmul" and "compute.matmul" not in cats:
            continue
        ins, _outs = _shape_samples(event)
        weight = _matmul_weight_shape(ins)
        if not weight:
            continue
        sig = (event.name_raw, event.task_type, tuple(int(dim) for dim in weight))
        if sig in signatures:
            continue
        signatures.add(sig)
        if len(examples) < 16:
            examples.append(
                {
                    "event_id": event.event_id,
                    "name": event.name_raw,
                    "task_type": event.task_type,
                    "weight_shape": list(weight),
                    "weight_elements": _shape_numel(weight),
                }
            )
    total = sum(_shape_numel(shape) for _name, _task, shape in signatures)
    return {
        "rank_visible_weight_param_lower_bound": total,
        "distinct_weight_signatures": len(signatures),
        "sample_weight_shapes": examples,
    }


def _infer_attention_shapes(events: Sequence[NormalizedEvent]) -> dict[str, Any]:
    head_dims: Counter[int] = Counter()
    q_heads: Counter[int] = Counter()
    kv_heads: Counter[int] = Counter()
    seq_lengths: Counter[int] = Counter()
    evidence: list[str] = []
    for event in events:
        cats = set(event.op_categories)
        if "attention.flash_score" not in cats and "attention.sparse_sharedkv" not in cats and "attention.linear_or_mamba" not in cats:
            continue
        ins, outs = _shape_samples(event)
        shapes = [*ins, *outs]
        for shape in shapes:
            if len(shape) >= 4:
                _add_candidate(head_dims, shape[-1], min_value=16, max_value=4096)
                _add_candidate(seq_lengths, shape[-2], min_value=1)
                _add_candidate(q_heads, shape[-3] if len(shape) > 4 else shape[1], min_value=1, max_value=4096)
        if len(ins) >= 2 and len(ins[0]) >= 4 and len(ins[1]) >= 4:
            _add_candidate(q_heads, ins[0][1], min_value=1, max_value=4096)
            _add_candidate(kv_heads, ins[1][1], min_value=1, max_value=4096)
        if len(evidence) < 16:
            evidence.append(event.event_id)
    return {
        "head_dim_candidates": _top_candidates(head_dims),
        "num_attention_heads_candidates": _top_candidates(q_heads),
        "num_key_value_heads_candidates": _top_candidates(kv_heads),
        "seq_len_candidates": _top_candidates(seq_lengths),
        "sample_event_ids": evidence,
    }


def _infer_matmul_shapes(events: Sequence[NormalizedEvent]) -> dict[str, Any]:
    hidden: Counter[int] = Counter()
    intermediate: Counter[int] = Counter()
    expert_counts: Counter[int] = Counter()
    evidence: list[str] = []
    for event in events:
        work = str(event.shape_features.get("estimated_work_class") or "")
        cats = set(event.op_categories)
        if work != "matmul" and "compute.matmul" not in cats:
            continue
        ins, outs = _shape_samples(event)
        if not ins:
            continue
        first = ins[0]
        second = ins[1] if len(ins) > 1 else []
        if len(first) >= 2:
            _add_candidate(hidden, first[-1], min_value=512, max_value=65536)
        if len(second) >= 2:
            _add_candidate(hidden, second[-2], min_value=512, max_value=65536)
            _add_candidate(intermediate, second[-1], min_value=512, max_value=262144)
        if len(second) >= 3 and ("moe.expert_matmul" in cats or "moe" in event.op_roles):
            _add_candidate(expert_counts, second[0], min_value=2, max_value=100000)
        for out in outs:
            if len(out) >= 2:
                _add_candidate(intermediate, out[-1], min_value=512, max_value=262144)
        if len(evidence) < 16:
            evidence.append(event.event_id)
    vocab = _infer_vocab_from_lm_head(events)
    params = _rank_visible_matmul_parameter_estimate(events)
    return {
        "hidden_size_candidates": _top_candidates(hidden),
        "intermediate_size_candidates": _top_candidates(intermediate),
        "num_experts_candidates": _top_candidates(expert_counts),
        "vocab_size_or_lm_head_shard_candidates": vocab["lm_head_name_hint_candidates"]
        or vocab["large_output_dim_candidates"],
        "lm_head_shape_inference": vocab,
        "parameter_estimate": params,
        "sample_event_ids": evidence,
    }


def _attention_feature_rows(events: Sequence[NormalizedEvent]) -> tuple[list[dict[str, Any]], list[str]]:
    cat_counter = Counter(cat for event in events for cat in event.op_categories)
    rows: list[dict[str, Any]] = []
    features: list[str] = []

    def add(feature: str, confidence: str, evidence_category: str, note: str) -> None:
        count = cat_counter.get(evidence_category, 0)
        if count <= 0:
            return
        features.append(feature)
        rows.append(
            {
                "feature": feature,
                "confidence": confidence,
                "evidence": evidence_category,
                "event_count": count,
                "note": note,
            }
        )

    add("mla", "medium", "attention.mla", "MLA prolog / preprocess / V-up projection kernels appeared in profiling.")
    add("kv_compressor", "high", "attention.kv_compressor", "KV compressor kernels appeared; exact compress ratio is not directly known from profiler rows.")
    add("dsa_or_csa_indexer", "high", "attention.lightning_indexer", "Lightning indexer kernels appeared.")
    add("sparse_sharedkv", "high", "attention.sparse_sharedkv", "Sparse shared-KV attention kernels appeared.")
    add("dense_flash_attention", "medium", "attention.flash_score", "Dense flash-style attention kernels appeared; MHA/GQA/MQA refinement needs Q/K shape sanity.")
    add("linear_attention_or_mamba", "high", "attention.linear_or_mamba", "Linear/Mamba/GDN family kernels appeared.")
    add("rope", "medium", "attention.rope", "RoPE companion kernels appeared.")

    cset = set(features)
    if {"kv_compressor", "dsa_or_csa_indexer", "sparse_sharedkv"} <= cset:
        rows.append(
            {
                "feature": "csa",
                "confidence": "medium",
                "evidence": "attention.kv_compressor + attention.lightning_indexer + attention.sparse_sharedkv",
                "event_count": min(cat_counter["attention.kv_compressor"], cat_counter["attention.lightning_indexer"], cat_counter["attention.sparse_sharedkv"]),
                "note": "Compressed Sparse Attention signature inferred from kernel combination.",
            }
        )
        features.append("csa")
    if "kv_compressor" in cset and "dense_flash_attention" in cset and "dsa_or_csa_indexer" not in cset:
        rows.append(
            {
                "feature": "hca",
                "confidence": "low",
                "evidence": "attention.kv_compressor + attention.flash_score",
                "event_count": min(cat_counter["attention.kv_compressor"], cat_counter["attention.flash_score"]),
                "note": "Heavily Compressed Attention signature is heuristic without per-layer config.",
            }
        )
        features.append("hca")
    return rows, list(dict.fromkeys(features))


def _layer_type_summary(layer_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: Counter[str] = Counter()
    for row in layer_rows:
        kinds = row.get("block_kinds")
        if isinstance(kinds, str):
            try:
                parsed = json.loads(kinds)
            except json.JSONDecodeError:
                parsed = [kinds]
        else:
            parsed = kinds or []
        key = "->".join(str(item) for item in parsed) if parsed else "unknown"
        grouped[key] += 1
    total = sum(grouped.values())
    return [
        {
            "layer_type": key,
            "observations": count,
            "share": round(count / total, 6) if total else 0.0,
        }
        for key, count in grouped.most_common()
    ]


def _field_row(field: str, candidates: Sequence[Mapping[str, Any]], confidence: str, evidence: str, note: str) -> dict[str, Any]:
    best = candidates[0] if candidates else {}
    return {
        "field": field,
        "inferred_value": best.get("value", "unknown"),
        "confidence": confidence if candidates else "unknown",
        "observations": best.get("observations", 0),
        "candidates": list(candidates),
        "evidence": evidence,
        "note": note if candidates else "not enough profiling shape evidence",
    }


def _scalar_field_row(field: str, value: Any, confidence: str, evidence: str, note: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    if value not in (None, "", "unknown"):
        candidates.append({"value": value, "observations": 1, "share": 1.0})
    return _field_row(field, candidates, confidence, evidence, note)


def _best_value(rows: Sequence[Mapping[str, Any]], field: str) -> Any:
    for row in rows:
        if row.get("field") == field:
            value = row.get("inferred_value")
            if value not in (None, "", "unknown"):
                return value
    return None


def _feature_matches(observed: set[str], expected: set[str]) -> tuple[int, list[str], list[str]]:
    matched = sorted(observed & expected)
    missing = sorted(expected - observed)
    return len(matched), matched, missing


def _field_hint_match(field: str, inferred_value: Any, values: Sequence[Any]) -> tuple[float, str | None]:
    inferred = _safe_i(inferred_value)
    if inferred is None:
        return 0.0, None
    expected_values = [_safe_i(item) for item in values]
    expected_values = [item for item in expected_values if item is not None]
    if not expected_values:
        return 0.0, None
    if inferred in expected_values:
        return 1.0, f"{field}={inferred}"
    if field in {"vocab_size", "vocab_size_or_lm_head_shard"}:
        for expected in expected_values:
            if expected and inferred > 0 and expected % inferred == 0:
                shard_factor = expected // inferred
                if 1 < shard_factor <= 128:
                    return 0.5, f"vocab_shard={inferred}x{shard_factor}->{expected}"
    return 0.0, None


def candidate_model_rows(
    inferred_rows: Sequence[Mapping[str, Any]],
    observed_features: Sequence[str],
    *,
    fingerprint_path: Path = MODEL_FINGERPRINTS_PATH,
) -> list[dict[str, Any]]:
    """Match profiling-derived fingerprints against a local candidate catalog."""

    models = load_model_fingerprints(fingerprint_path)
    observed = set(observed_features)
    inferred_by_field = {
        "expected_layers": _best_value(inferred_rows, "num_hidden_layers"),
        "hidden_size": _best_value(inferred_rows, "hidden_size"),
        "intermediate_size": _best_value(inferred_rows, "intermediate_size_or_moe_intermediate"),
        "num_experts": _best_value(inferred_rows, "num_experts"),
        "head_dim": _best_value(inferred_rows, "head_dim"),
        "num_attention_heads": _best_value(inferred_rows, "num_attention_heads"),
        "num_key_value_heads": _best_value(inferred_rows, "num_key_value_heads"),
        "profile_seq_len_common": _best_value(inferred_rows, "profile_seq_len"),
        "vocab_size": _best_value(inferred_rows, "vocab_size_or_lm_head_shard"),
        "vocab_size_or_lm_head_shard": _best_value(inferred_rows, "vocab_size_or_lm_head_shard"),
    }
    out: list[dict[str, Any]] = []
    for model in models:
        score = 0.0
        max_score = 0.0
        reasons: list[str] = []
        expected_features = set(str(item) for item in model.get("features") or [])
        f_match, matched, missing = _feature_matches(observed, expected_features)
        if expected_features:
            max_score += len(expected_features) * 3.0
            score += f_match * 3.0
            if matched:
                reasons.append("features=" + ",".join(matched))
            if missing:
                reasons.append("missing=" + ",".join(missing))
        expected_layers = model.get("expected_layers")
        inferred_layers = inferred_by_field.get("expected_layers")
        if expected_layers is not None:
            max_score += 2.0
            inferred_layers_i = _safe_i(inferred_layers)
            expected_layers_i = _safe_i(expected_layers)
            if inferred_layers_i is not None and expected_layers_i is not None and inferred_layers_i == expected_layers_i:
                score += 2.0
                reasons.append(f"layers={expected_layers}")
            elif inferred_layers is not None:
                reasons.append(f"layers_mismatch(profile={inferred_layers}, catalog={expected_layers})")
        field_hints = model.get("field_hints") or {}
        if isinstance(field_hints, Mapping):
            for field, values in field_hints.items():
                if not isinstance(values, list):
                    values = [values]
                max_score += 1.0
                inferred_value = inferred_by_field.get(str(field))
                if inferred_value is None:
                    continue
                match_score, reason = _field_hint_match(str(field), inferred_value, values)
                if match_score > 0:
                    score += match_score
                    if reason:
                        reasons.append(reason)
        confidence = "low"
        if max_score > 0:
            ratio = score / max_score
            if ratio >= 0.75:
                confidence = "high"
            elif ratio >= 0.45:
                confidence = "medium"
        out.append(
            {
                "model_name": model.get("model_name"),
                "aliases": model.get("aliases") or [],
                "family": model.get("family"),
                "score": round(score, 3),
                "max_score": round(max_score, 3),
                "match_ratio": round(score / max_score, 6) if max_score else 0.0,
                "confidence": confidence,
                "matched_reasons": reasons,
                "source_note": model.get("source_note"),
            }
        )
    out.sort(key=lambda item: (-float(item.get("score") or 0.0), str(item.get("model_name") or "")))
    return out


def _profile_feature_set(
    feature_rows: Sequence[Mapping[str, Any]],
    layer_type_rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    features = [str(row.get("feature")) for row in feature_rows if row.get("feature")]
    if any("moe" in str(row.get("layer_type") or "") for row in layer_type_rows):
        features.append("moe")
    return list(dict.fromkeys(features))


def profile_inferred_model_insights(
    events: Sequence[NormalizedEvent],
    step_rows: Sequence[Mapping[str, Any]],
    layer_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Infer model-architecture hints directly from profiling artifacts."""

    attention_shape = _infer_attention_shapes(events)
    matmul_shape = _infer_matmul_shapes(events)
    feature_rows, features = _attention_feature_rows(events)
    layer_type_rows = _layer_type_summary(layer_rows)
    features = _profile_feature_set(feature_rows, layer_type_rows)
    layer_count_candidates: Counter[int] = Counter()
    for row in step_rows:
        if row.get("segment_type") != "step":
            continue
        _add_candidate(layer_count_candidates, row.get("main_layer_count"), min_value=1, max_value=100000)

    inferred_rows = [
        _field_row("num_hidden_layers", _top_candidates(layer_count_candidates), "medium", "step_summary.main_layer_count", "Mode of complete step layer counts."),
        _field_row("hidden_size", matmul_shape["hidden_size_candidates"], "low", "matmul Input Shapes", "Most common shared / activation dimension across matmul-like kernels."),
        _field_row("intermediate_size_or_moe_intermediate", matmul_shape["intermediate_size_candidates"], "low", "matmul Output/Input Shapes", "Most common large N/output dimension in matmul-like kernels."),
        _field_row("num_experts", matmul_shape["num_experts_candidates"], "medium", "GroupedMatmul weight Input Shapes", "First dimension of 3D GroupedMatmul expert weight tensors when present."),
        _field_row("vocab_size_or_lm_head_shard", matmul_shape["vocab_size_or_lm_head_shard_candidates"], "medium", "lm_head/logits matmul shapes", "Large lm_head/logits output dimension. On tensor-parallel runs this may be a vocab shard, not global vocab_size."),
        _scalar_field_row(
            "rank_visible_matmul_weight_params_lower_bound",
            matmul_shape["parameter_estimate"].get("rank_visible_weight_param_lower_bound"),
            "low",
            "distinct matmul weight Input Shapes",
            "Lower-bound count of distinct rank-visible matmul weight elements; global parameters require TP/EP/DP strategy.",
        ),
        _field_row("head_dim", attention_shape["head_dim_candidates"], "medium", "attention Input Shapes", "Last dimension of attention Q/K/V-like tensors."),
        _field_row("num_attention_heads", attention_shape["num_attention_heads_candidates"], "medium", "attention Input Shapes", "Head dimension from first attention input tensor."),
        _field_row("num_key_value_heads", attention_shape["num_key_value_heads_candidates"], "medium", "attention Input Shapes", "Head dimension from second attention input tensor when CANN emits it."),
        _field_row("profile_seq_len", attention_shape["seq_len_candidates"], "high", "attention Input Shapes", "Observed profiled sequence lengths, not model max_position_embeddings."),
    ]

    limitations = [
        "profiling can infer vocab candidates only when lm_head/logits shapes are visible; tensor-parallel runs may expose only a vocab shard",
        "profiling cannot prove tokenizer ids, rope_theta, initializer_range, or model_type",
        "shape fields may be absent or wiped by graph capture; candidates are best-effort",
        "single-rank parameter estimates are rank-visible lower bounds; global parameters require TP/EP/DP and weight-sharding strategy",
        "profile_seq_len is workload length, not model maximum context length",
    ]
    candidate_rows = candidate_model_rows(inferred_rows, features)
    return {
        "available": True,
        "source": "profiling",
        "inferred_config_rows": inferred_rows,
        "attention_feature_rows": feature_rows,
        "layer_type_rows": layer_type_rows,
        "candidate_model_rows": candidate_rows,
        "features": features,
        "shape_inference": {
            "attention": attention_shape,
            "matmul": matmul_shape,
        },
        "limitations": limitations,
    }


def model_config_insights(config_path: Path | None) -> dict[str, Any]:
    if config_path is None:
        return {
            "available": False,
            "reason": "no model config supplied",
            "overview_rows": [],
            "parameter_rows": [],
            "kv_cache_rows": [],
            "feature_rows": [],
        }
    if not config_path.is_file():
        return {
            "available": False,
            "reason": f"model config not found: {config_path}",
            "overview_rows": [],
            "parameter_rows": [],
            "kv_cache_rows": [],
            "feature_rows": [],
        }
    try:
        root = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {
            "available": False,
            "reason": f"model config is not valid JSON: {config_path} ({exc})",
            "overview_rows": [],
            "parameter_rows": [],
            "kv_cache_rows": [],
            "feature_rows": [],
        }
    if not isinstance(root, Mapping):
        return {
            "available": False,
            "reason": f"model config is not a JSON object: {config_path}",
            "overview_rows": [],
            "parameter_rows": [],
            "kv_cache_rows": [],
            "feature_rows": [],
        }
    cfg = text_config(root)
    model_name = config_path.parent.name or str(first_present(root, "model_type", default="unknown"))
    layers = to_int(first_present(cfg, "num_hidden_layers", "n_layers", default=0))
    layer_counts = _layer_kind_counts(cfg, layers)
    features = _attention_features(root, cfg)
    parameter_rows, parameter_totals = _parameter_rows(root, cfg)
    kv_rows = _kv_rows(cfg, features)
    feature_rows = _feature_rows(root, cfg, features)
    overview_rows = [
        {"key": "model_name", "value": model_name},
        {"key": "model_type", "value": first_present(cfg, "model_type", default=first_present(root, "model_type", default="unknown"))},
        {"key": "architectures", "value": root.get("architectures", [])},
        {"key": "hidden_size", "value": first_present(cfg, "hidden_size", "dim", default=None)},
        {"key": "num_layers", "value": layers},
        {"key": "num_attention_heads", "value": first_present(cfg, "num_attention_heads", "n_heads", default=None)},
        {"key": "num_key_value_heads", "value": first_present(cfg, "num_key_value_heads", "n_kv_heads", default=None)},
        {"key": "attention_features", "value": features},
        {"key": "layer_type_counts", "value": dict(layer_counts)},
        {"key": "total_params_estimate", "value": _fmt_params(parameter_totals["total_params"])},
        {"key": "active_params_estimate", "value": _fmt_params(parameter_totals["active_params"])},
    ]
    return {
        "available": True,
        "source": str(config_path),
        "overview_rows": overview_rows,
        "parameter_rows": parameter_rows,
        "parameter_totals": parameter_totals,
        "kv_cache_rows": kv_rows,
        "feature_rows": feature_rows,
        "attention_features": features,
        "layer_type_counts": dict(layer_counts),
    }


def operator_efficiency_rows(
    events: Sequence[NormalizedEvent],
    hardware: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[NormalizedEvent]] = defaultdict(list)
    for event in events:
        role_key = ",".join(event.op_roles) or "unknown"
        grouped[(event.name_raw, event.task_type, event.op_type, role_key)].append(event)

    rows: list[dict[str, Any]] = []
    for (name, task, op_type, roles), items in grouped.items():
        duration_us = sum(float(event.duration_us) for event in items)
        if duration_us <= 0:
            continue
        flops = sum(to_float(event.shape_features.get("estimated_flops")) for event in items)
        bytes_est = sum(to_float(event.shape_features.get("estimated_bytes")) for event in items)
        classes = Counter(str(event.shape_features.get("estimated_work_class") or "unknown") for event in items)
        dtype_counts = Counter(str(event.shape_features.get("estimated_dtype") or "") for event in items if event.shape_features.get("estimated_dtype"))
        work_class = classes.most_common(1)[0][0] if classes else "unknown"
        dtype = dtype_counts.most_common(1)[0][0] if dtype_counts else ""
        duration_s = duration_us * 1e-6
        achieved_tflops = flops / duration_s / 1e12 if flops > 0 else None
        achieved_gbps = bytes_est / duration_s / 1e9 if bytes_est > 0 else None
        theoretical_peak, theoretical_peak_source = peak_flops_per_second(
            hardware,
            work_class=work_class,
            dtype=dtype,
            sustained=False,
        )
        sustained_peak, sustained_peak_source = peak_flops_per_second(
            hardware,
            work_class=work_class,
            dtype=dtype,
            sustained=True,
        )
        peak_bw, peak_bw_source = memory_bandwidth_bytes_per_second(hardware)
        compute_theory_s = flops / theoretical_peak if flops > 0 and theoretical_peak > 0 else 0.0
        compute_sustained_s = flops / sustained_peak if flops > 0 and sustained_peak > 0 else 0.0
        memory_s = bytes_est / peak_bw if bytes_est > 0 and peak_bw > 0 else 0.0
        ideal_theory_us = max(compute_theory_s, memory_s) * 1e6
        ideal_sustained_us = max(compute_sustained_s, memory_s) * 1e6
        modeled = flops > 0 or bytes_est > 0
        reclaim_theory_us = max(0.0, duration_us - ideal_theory_us) if modeled else 0.0
        reclaim_sustained_us = max(0.0, duration_us - ideal_sustained_us) if modeled else 0.0
        ai = flops / bytes_est if flops > 0 and bytes_est > 0 else None
        durations = sorted(float(event.duration_us) for event in items)
        hardware_summary = hardware.get("summary") if isinstance(hardware, Mapping) and isinstance(hardware.get("summary"), Mapping) else {}
        rows.append(
            {
                "name": name,
                "task_type": task,
                "op_type": op_type,
                "roles": roles,
                "work_class": work_class,
                "dtype": dtype,
                "call_count": len(items),
                "duration_sum_us": round(duration_us, 3),
                "duration_p50_us": round(quantile(durations, 0.5), 3),
                "estimated_flops": round(flops, 3) if flops > 0 else None,
                "estimated_bytes": round(bytes_est, 3) if bytes_est > 0 else None,
                "arithmetic_intensity": round(ai, 6) if ai is not None else None,
                "achieved_tflops": round(achieved_tflops, 6) if achieved_tflops is not None else None,
                "achieved_gbps": round(achieved_gbps, 6) if achieved_gbps is not None else None,
                "hardware_model": hardware_summary.get("hardware_model"),
                "theoretical_peak_source": theoretical_peak_source,
                "sustained_peak_source": sustained_peak_source,
                "memory_bandwidth_source": peak_bw_source,
                "theoretical_peak_tflops_or_tops": round(theoretical_peak / 1e12, 6) if theoretical_peak > 0 else None,
                "sustained_peak_tflops_or_tops": round(sustained_peak / 1e12, 6) if sustained_peak > 0 else None,
                "ideal_us_theoretical": round(ideal_theory_us, 3) if modeled and (theoretical_peak > 0 or peak_bw > 0) else None,
                "ideal_us_sustained": round(ideal_sustained_us, 3) if modeled and (sustained_peak > 0 or peak_bw > 0) else None,
                "reclaim_us_theoretical": round(reclaim_theory_us, 3) if modeled and (theoretical_peak > 0 or peak_bw > 0) else None,
                "reclaim_us_sustained": round(reclaim_sustained_us, 3) if modeled and (sustained_peak > 0 or peak_bw > 0) else None,
                "roofline_efficiency_theoretical": round(min(ideal_theory_us / duration_us, 1.0), 6)
                if modeled and duration_us > 0 and (theoretical_peak > 0 or peak_bw > 0)
                else None,
                "roofline_efficiency_sustained": round(min(ideal_sustained_us / duration_us, 1.0), 6)
                if modeled and duration_us > 0 and (sustained_peak > 0 or peak_bw > 0)
                else None,
                "mfu_theoretical": round(achieved_tflops / (theoretical_peak / 1e12), 6)
                if achieved_tflops is not None and theoretical_peak > 0
                else None,
                "sustained_efficiency": round(achieved_tflops / (sustained_peak / 1e12), 6)
                if achieved_tflops is not None and sustained_peak > 0
                else None,
                "confidence": "shape_estimate" if modeled else "unmodeled",
                "sample_event_ids": [event.event_id for event in items[:16]],
            }
        )
    rows.sort(key=lambda item: -float(item.get("reclaim_us_sustained") or item.get("reclaim_us_theoretical") or 0.0))
    return rows
