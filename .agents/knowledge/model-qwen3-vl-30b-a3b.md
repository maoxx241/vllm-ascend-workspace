# Qwen3-VL-30B-A3B: 48-layer dense-attention MoE VLM (decoder layers; vision encoder excluded); verified TP2 on A3

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-03. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Recorded model structure

model_type qwen3_vl_moe_text text_config: 48 decoder layers (hidden 2048, 128 experts); the vision encoder is not part of the decoder layer count.

## Profiling observation

Recorded model structure from workspace profiling validation: expected_layers=48; attention_family=dense_flash_attention, moe, moe, dense_flash_attention, rope

## Usage context

Use the recorded structure when planning collection/analysis preflight. Verified configs: tp2/enforce_eager/A3.

## Search terms

- qwen3-vl-30b-a3b
- qwen3-vl-30b-a3b-instruct
- qwen3_vl_moe
- qwen/qwen3-vl-30b-a3b-instruct

## Recorded context

- soc: A3.
- model: model-qwen3-vl-30b-a3b, qwen/qwen3-vl-30b-a3b-instruct, qwen3-vl-30b-a3b, qwen3-vl-30b-a3b-instruct, qwen3_vl_moe.
- topology: tp2.
- execution mode: eager.
- component: model-capability, profiling-preflight.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-qwen3-vl-30b-a3b; first observed: 2026-09-03.
