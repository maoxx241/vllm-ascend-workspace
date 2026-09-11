# GLM-5: DSA sparse-attention MoE (same structure family as DeepSeek-V3.2: lightning indexer + sparse shared-KV, no KV compressor); layer count config-driven

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-03. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

Known conditions and unknown dimensions are preserved below. Recheck actual code, model configuration and runtime facts before applying this note.

## Symptom

User-confirmed 2026-09: GLM-5 shares the DSA (DeepSeek-V3.2) sparse-attention structure. expected_layers stays null on purpose: read num_hidden_layers from the model's config.json instead of guessing; replace with a verified count once a GLM-5 profile is collected.

## Root cause

Recorded model structure from workspace profiling validation: expected_layers=78

## Resolution

Use the recorded structure when planning collection/analysis preflight.

## Fingerprints

- glm-5
- glm5
- glm5forcausallm

## Recorded conditions

{
  "soc": {
    "range": {
      "min": null,
      "max": null
    }
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
      "glm-5",
      "glm5",
      "glm5forcausallm",
      "model-glm-5"
    ]
  },
  "topology": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "execution_mode": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "component": {
    "values": [
      "model-capability",
      "profiling-preflight"
    ]
  }
}

## Provenance

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-glm-5; first observed: 2026-09-03.
