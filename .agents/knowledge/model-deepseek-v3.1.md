# DeepSeek-V3.1: 61-layer MLA + MoE model; verified TP8 on A3 with the w4a8-mtp-QuaRot quantized checkpoint (MTP speculative decoding)

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-03. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

Known conditions and unknown dimensions are preserved below. Recheck actual code, model configuration and runtime facts before applying this note.

## Symptom

model_type deepseek_v3: 61 layers (hidden 7168, 128 heads, 256 experts, first_k_dense_replace 3). Quantization variants (w4a8/w8a8/QuaRot) do not change structural fields; the MTP head is speculative, not part of the 61-layer main body.

## Root cause

Recorded model structure from workspace profiling validation: expected_layers=61; attention_family=mla, moe, moe, mla

## Resolution

Use the recorded structure when planning collection/analysis preflight. Verified configs: tp8/enforce_eager/A3.

## Fingerprints

- deepseek-v3.1
- deepseek-v3-1
- deepseek_v3
- deepseek-ai/deepseek-v3.1

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
      "deepseek-ai/deepseek-v3.1",
      "deepseek-v3-1",
      "deepseek-v3.1",
      "deepseek_v3",
      "model-deepseek-v3.1"
    ]
  },
  "topology": {
    "values": [
      "tp8"
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

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-deepseek-v3.1; first observed: 2026-09-03.
