# DeepSeek-V2-Lite: 27-layer MLA + MoE model; verified TP1 on A3

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-03. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

Known conditions and unknown dimensions are preserved below. Recheck actual code, model configuration and runtime facts before applying this note.

## Symptom

Standard 27-layer MLA+MoE body (hidden 2048, 16 attention heads, 64 experts). MTP/Eagle-style short speculative bodies must not enter the step layer inventory.

## Root cause

Recorded model structure from workspace profiling validation: expected_layers=27; attention_family=mla, moe, moe, mla

## Resolution

Use the recorded structure when planning collection/analysis preflight. Verified configs: tp1/enforce_eager/A3.

## Fingerprints

- deepseek-v2-lite
- dsv2-lite
- dsv2lite
- deepseekv2liteforcausallm

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
      "deepseek-v2-lite",
      "deepseekv2liteforcausallm",
      "dsv2-lite",
      "dsv2lite",
      "model-deepseek-v2-lite"
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

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-deepseek-v2-lite; first observed: 2026-09-03.
