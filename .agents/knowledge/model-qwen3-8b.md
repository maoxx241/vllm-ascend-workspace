# Qwen3-8B: 36-layer dense GQA model (dense flash attention + RoPE, no MoE); verified TP1 enforce_eager on A3

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-03. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Recorded model structure

Dense GQA body (model_type qwen3): hidden 4096, 32 attention heads, 8 KV heads, head_dim 128. Layer count verified from the shared-storage config.json and confirmed by profile-visible layer inventory.

## Profiling observation

Recorded model structure from workspace profiling validation: expected_layers=36; attention_family=dense_flash_attention, dense, dense_flash_attention, rope

## Usage context

Use the recorded structure when planning collection/analysis preflight. Verified configs: tp1/enforce_eager/A3.

## Search terms

- qwen3-8b
- qwen/qwen3-8b

## Recorded context

- soc: A3.
- model: model-qwen3-8b, qwen/qwen3-8b, qwen3-8b.
- topology: tp1.
- execution mode: eager.
- component: model-capability, profiling-preflight.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-qwen3-8b; first observed: 2026-09-03.
