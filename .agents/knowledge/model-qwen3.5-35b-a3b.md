# Qwen3.5-35B-A3B: 40-layer hybrid linear/full-attention MoE model; verified TP2 on A3

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-04. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

Known conditions and unknown dimensions are preserved below. Recheck actual code, model configuration and runtime facts before applying this note.

## Symptom

model_type qwen3_5_moe_text: 40 decoder layers in a repeating 3x linear_attention + 1x full_attention hybrid pattern; 256 experts, head_dim 256.

## Root cause

Recorded model structure from workspace profiling validation: expected_layers=40; attention_family=hybrid_linear_attention, moe, moe, linear_attention_or_mamba, dense_flash_attention, rope

## Resolution

Use the recorded structure when planning collection/analysis preflight. Verified configs: tp2/enforce_eager/A3.

## Fingerprints

- qwen3.5-35b-a3b
- qwen35-35b
- qwen/qwen3.5-35b-a3b

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
      "model-qwen3.5-35b-a3b",
      "qwen/qwen3.5-35b-a3b",
      "qwen3.5-35b-a3b",
      "qwen35-35b"
    ]
  },
  "topology": {
    "values": [
      "tp2"
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

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-qwen3.5-35b-a3b; first observed: 2026-09-04.
