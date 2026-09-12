# Qwen3.5-35B-A3B: 40-layer hybrid linear/full-attention MoE model; verified TP2 on A3

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-04. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Recorded model structure

model_type qwen3_5_moe_text: 40 decoder layers in a repeating 3x linear_attention + 1x full_attention hybrid pattern; 256 experts, head_dim 256.

## Profiling observation

Recorded model structure from workspace profiling validation: expected_layers=40; attention_family=hybrid_linear_attention, moe, moe, linear_attention_or_mamba, dense_flash_attention, rope

## Usage context

Use the recorded structure when planning collection/analysis preflight. Verified configs: tp2/enforce_eager/A3.

## Search terms

- qwen3.5-35b-a3b
- qwen35-35b
- qwen/qwen3.5-35b-a3b

## Recorded context

- soc: A3.
- model: model-qwen3.5-35b-a3b, qwen/qwen3.5-35b-a3b, qwen3.5-35b-a3b, qwen35-35b.
- topology: tp2.
- execution mode: eager.
- component: model-capability, profiling-preflight.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-qwen3.5-35b-a3b; first observed: 2026-09-04.
