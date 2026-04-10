#!/usr/bin/env python3
"""
Remote weight inspector -- reads safetensors file headers to extract
exact tensor shapes, dtypes, and sizes. Designed to run ON the remote
machine where model weights live.

Input: model directory path
Output: JSON on stdout with full tensor manifest
"""
from __future__ import annotations

import glob
import json
import os
import struct
import sys
from pathlib import Path

DTYPE_SIZES = {
    "F64": 8, "F32": 4, "F16": 2, "BF16": 2,
    "F8_E5M2": 1, "F8_E4M3": 1, "I64": 8, "I32": 4,
    "I16": 2, "I8": 1, "U8": 1, "BOOL": 1,
}


def read_safetensors_header(filepath: str) -> dict:
    """Read the JSON header from a safetensors file."""
    with open(filepath, "rb") as f:
        header_len_bytes = f.read(8)
        if len(header_len_bytes) < 8:
            return {}
        header_len = struct.unpack("<Q", header_len_bytes)[0]
        if header_len > 100_000_000:
            return {}
        header_bytes = f.read(header_len)
        return json.loads(header_bytes)


def parse_tensor_info(header: dict) -> list[dict]:
    """Extract tensor metadata from a safetensors header."""
    tensors = []
    for name, info in header.items():
        if name == "__metadata__":
            continue
        dtype = info.get("dtype", "F16")
        shape = info.get("shape", [])
        offsets = info.get("data_offsets", [0, 0])
        byte_size = offsets[1] - offsets[0] if len(offsets) == 2 else 0
        numel = 1
        for d in shape:
            numel *= d
        tensors.append({
            "name": name,
            "dtype": dtype,
            "shape": shape,
            "numel": numel,
            "byte_size": byte_size,
            "bytes_per_element": DTYPE_SIZES.get(dtype, 2),
        })
    return tensors


def classify_tensor(name: str) -> str:
    """Classify a tensor name into a component category."""
    n = name.lower()

    if "visual" in n or "vision" in n or "vit." in n or "image_encoder" in n:
        return "vision"

    # MTP (multi-token prediction) — further sub-classify
    if "mtp" in n:
        if ".experts." in n and "shared_expert" not in n:
            return "mtp_expert"
        if "shared_expert" in n:
            return "mtp_shared_expert"
        if ".gate." in n or "shared_expert_gate" in n:
            return "mtp_gate"
        if "norm" in n:
            return "mtp_norm"
        if ".self_attn." in n or ".attention." in n:
            return "mtp_attn"
        if ".fc." in n:
            return "mtp_fc"
        return "mtp_other"

    if "embed_tokens" in n or "wte." in n:
        return "embedding"
    if "lm_head" in n:
        return "lm_head"

    # Linear / Mamba-style attention (hybrid architectures like Qwen3.5)
    if ".linear_attn." in n:
        if "in_proj_qkv" in n:
            return "linear_attn_qkv"
        if "in_proj_z" in n or "out_proj" in n:
            return "linear_attn_proj"
        if "conv" in n:
            return "linear_attn_conv"
        return "linear_attn_param"

    # Standard self-attention
    if ".self_attn." in n or ".attention." in n or ".attn." in n:
        if "q_proj" in n or "q_a_proj" in n or "q_b_proj" in n:
            return "attn_q"
        if "k_proj" in n or "k_a_proj" in n or "k_b_proj" in n:
            return "attn_k"
        if "v_proj" in n or "v_a_proj" in n or "v_b_proj" in n:
            return "attn_v"
        if "o_proj" in n or "out_proj" in n:
            return "attn_o"
        if "conv" in n:
            return "attn_conv"
        if "kv_a_layernorm" in n or "kv_b_proj" in n:
            return "attn_kv_compress"
        return "attn_other"

    if ".mlp." in n or ".feed_forward." in n or ".ffn." in n:
        if "expert" in n:
            if "shared_expert" in n:
                return "moe_shared_expert"
            return "moe_expert"
        if "gate" in n and ("proj" not in n and "up" not in n and "down" not in n):
            return "moe_gate"
        return "ffn"

    if "block_sparse_moe" in n or "moe" in n:
        if "expert" in n:
            if "shared" in n:
                return "moe_shared_expert"
            return "moe_expert"
        if "gate" in n:
            return "moe_gate"
        return "moe_other"

    if "layernorm" in n or "layer_norm" in n or "norm" in n or "ln_" in n:
        return "norm"

    if "scale" in n or "zero_point" in n or "quant_" in n:
        return "quant_param"

    return "other"


SHARD_COL_PARALLEL = "col_parallel"
SHARD_ROW_PARALLEL = "row_parallel"
SHARD_EXPERT_PARALLEL = "expert_parallel"
SHARD_REPLICATED = "replicated"


def classify_shard_strategy(name: str, category: str) -> str:
    """Determine how a tensor is sharded across TP/EP devices."""
    if category in ("norm", "quant_param", "moe_gate", "attn_conv", "attn_other"):
        return SHARD_REPLICATED

    if category == "moe_expert":
        return SHARD_EXPERT_PARALLEL

    if category in ("attn_q", "attn_k", "attn_v", "attn_kv_compress"):
        return SHARD_COL_PARALLEL
    if category == "attn_o":
        return SHARD_ROW_PARALLEL

    # Linear attention (Mamba-style hybrid)
    if category == "linear_attn_qkv":
        return SHARD_COL_PARALLEL
    if category == "linear_attn_proj":
        n = name.lower()
        if "out_proj" in n:
            return SHARD_ROW_PARALLEL
        return SHARD_COL_PARALLEL
    if category in ("linear_attn_conv", "linear_attn_param"):
        return SHARD_COL_PARALLEL

    if category == "ffn" or category == "moe_shared_expert":
        n = name.lower()
        if "gate_proj" in n or "up_proj" in n or "w1" in n or "w3" in n:
            return SHARD_COL_PARALLEL
        if "down_proj" in n or "w2" in n:
            return SHARD_ROW_PARALLEL
        return SHARD_COL_PARALLEL

    if category == "embedding":
        return SHARD_COL_PARALLEL
    if category == "lm_head":
        return SHARD_COL_PARALLEL

    if category == "vision":
        n = name.lower()
        if "norm" in n or "patch_embed" in n or "pos_embed" in n:
            return SHARD_REPLICATED
        if "qkv" in n or "linear_fc1" in n or "gate_up" in n:
            return SHARD_COL_PARALLEL
        if "proj.weight" in n or "proj.bias" in n or "linear_fc2" in n or "down_proj" in n:
            return SHARD_ROW_PARALLEL
        return SHARD_COL_PARALLEL

    if category == "mtp_expert":
        return SHARD_EXPERT_PARALLEL
    if category == "mtp_shared_expert":
        n = name.lower()
        if "down_proj" in n or "w2" in n:
            return SHARD_ROW_PARALLEL
        return SHARD_COL_PARALLEL
    if category in ("mtp_gate", "mtp_norm"):
        return SHARD_REPLICATED
    if category == "mtp_attn":
        n = name.lower()
        if "o_proj" in n or "out_proj" in n:
            return SHARD_ROW_PARALLEL
        return SHARD_COL_PARALLEL
    if category in ("mtp_fc", "mtp_other"):
        return SHARD_COL_PARALLEL

    return SHARD_REPLICATED


def main():
    if len(sys.argv) < 2:
        print("Usage: weight_inspector.py <model_dir>", file=sys.stderr)
        sys.exit(1)

    model_dir = sys.argv[1]

    st_files = sorted(glob.glob(os.path.join(model_dir, "*.safetensors")))
    if not st_files:
        print(json.dumps({"error": "no safetensors files found"}))
        sys.exit(1)

    all_tensors = []
    for sf in st_files:
        header = read_safetensors_header(sf)
        tensors = parse_tensor_info(header)
        for t in tensors:
            t["source_file"] = os.path.basename(sf)
            t["category"] = classify_tensor(t["name"])
            t["shard_strategy"] = classify_shard_strategy(t["name"], t["category"])
        all_tensors.extend(tensors)

    categories = {}
    for t in all_tensors:
        cat = t["category"]
        if cat not in categories:
            categories[cat] = {
                "total_bytes": 0, "total_params": 0,
                "tensor_count": 0, "dtypes": set()
            }
        categories[cat]["total_bytes"] += t["byte_size"]
        categories[cat]["total_params"] += t["numel"]
        categories[cat]["tensor_count"] += 1
        categories[cat]["dtypes"].add(t["dtype"])

    for cat in categories:
        categories[cat]["dtypes"] = sorted(categories[cat]["dtypes"])

    total_bytes = sum(t["byte_size"] for t in all_tensors)
    total_params = sum(t["numel"] for t in all_tensors)

    result = {
        "model_dir": model_dir,
        "num_safetensors_files": len(st_files),
        "total_tensors": len(all_tensors),
        "total_bytes": total_bytes,
        "total_params": total_params,
        "total_gib": round(total_bytes / (1024**3), 4),
        "categories": categories,
        "tensors": all_tensors,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
