# DeepSeek-V4-Pro: 61-layer sparse-attention MoE model (KV compressor + DSA/CSA indexer); verified TP16 on A3; needs gpu-memory-utilization 0.95

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-03. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

Known conditions and unknown dimensions are preserved below. Recheck actual code, model configuration and runtime facts before applying this note.

## Symptom

hidden 7168, 128 heads, head_dim 512, 384 experts. Serving constraint: use gpu-memory-utilization=0.95 on A3 (the default 0.85 leaves too little headroom for this checkpoint).

## Root cause

Recorded model structure from workspace profiling validation: expected_layers=61; attention_family=dsa_csa_sparse, moe, moe, kv_compressor, dsa_or_csa_indexer, sparse_sharedkv, csa, hca

## Resolution

Use the recorded structure when planning collection/analysis preflight. Verified configs: tp16/enforce_eager/A3.

## Fingerprints

- deepseek-v4-pro
- deepseekv4-pro
- deepseek-ai/deepseek-v4-pro

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
      "deepseek-ai/deepseek-v4-pro",
      "deepseek-v4-pro",
      "deepseekv4-pro",
      "model-deepseek-v4-pro"
    ]
  },
  "topology": {
    "values": [
      "tp16"
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

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-deepseek-v4-pro; first observed: 2026-09-03.
