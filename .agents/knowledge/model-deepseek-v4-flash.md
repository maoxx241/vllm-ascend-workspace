# DeepSeek-V4-Flash: 43-layer sparse-attention MoE model (KV compressor + DSA/CSA indexer); verified TP4 on A3

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-03. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

Known conditions and unknown dimensions are preserved below. Recheck actual code, model configuration and runtime facts before applying this note.

## Symptom

Profile-visible main body is 43 layers (hidden 4096, 64 heads, head_dim 512, 256 experts). Known MTP/Eagle-like short bodies must not enter the step inventory until an explicit speculative-layer template exists.

## Root cause

Recorded model structure from workspace profiling validation: expected_layers=43; attention_family=dsa_csa_sparse, moe, moe, kv_compressor, dsa_or_csa_indexer, sparse_sharedkv, csa, hca

## Resolution

Use the recorded structure when planning collection/analysis preflight. Verified configs: tp4/enforce_eager/A3.

## Fingerprints

- deepseek-v4-flash
- deepseekv4-flash
- deepseek-ai/deepseek-v4-flash

## Recorded conditions

{
  "soc": {
    "values": [
      "A3"
    ]
  },
  "cann": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "driver": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "python_abi": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "torch": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "torch_npu": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "vllm": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "vllm_ascend": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "model": {
    "values": [
      "deepseek-ai/deepseek-v4-flash",
      "deepseek-v4-flash",
      "deepseekv4-flash",
      "model-deepseek-v4-flash"
    ]
  },
  "topology": {
    "values": [
      "tp4"
    ]
  },
  "execution_mode": {
    "values": [
      "eager"
    ]
  },
  "component": {
    "values": [
      "model-capability",
      "profiling-preflight"
    ]
  }
}

## Provenance

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-deepseek-v4-flash; first observed: 2026-09-03.
