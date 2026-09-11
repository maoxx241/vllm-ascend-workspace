# Qwen3-8B: 36-layer dense GQA model (dense flash attention + RoPE, no MoE); verified TP1 enforce_eager on A3

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-03. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

Known conditions and unknown dimensions are preserved below. Recheck actual code, model configuration and runtime facts before applying this note.

## Symptom

Dense GQA body (model_type qwen3): hidden 4096, 32 attention heads, 8 KV heads, head_dim 128. Layer count verified from the shared-storage config.json and confirmed by profile-visible layer inventory.

## Root cause

Recorded model structure from workspace profiling validation: expected_layers=36; attention_family=dense_flash_attention, dense, dense_flash_attention, rope

## Resolution

Use the recorded structure when planning collection/analysis preflight. Verified configs: tp1/enforce_eager/A3.

## Fingerprints

- qwen3-8b
- qwen/qwen3-8b

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
      "model-qwen3-8b",
      "qwen/qwen3-8b",
      "qwen3-8b"
    ]
  },
  "topology": {
    "values": [
      "tp1"
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

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-qwen3-8b; first observed: 2026-09-03.
