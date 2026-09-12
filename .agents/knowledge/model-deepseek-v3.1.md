# DeepSeek-V3.1: 61-layer MLA + MoE model; verified TP8 on A3 with the w4a8-mtp-QuaRot quantized checkpoint (MTP speculative decoding)

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-03. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Recorded model structure

model_type deepseek_v3: 61 layers (hidden 7168, 128 heads, 256 experts, first_k_dense_replace 3). Quantization variants (w4a8/w8a8/QuaRot) do not change structural fields; the MTP head is speculative, not part of the 61-layer main body.

## Profiling observation

Recorded model structure from workspace profiling validation: expected_layers=61; attention_family=mla, moe, moe, mla

## Usage context

Use the recorded structure when planning collection/analysis preflight. Verified configs: tp8/enforce_eager/A3.

## Search terms

- deepseek-v3.1
- deepseek-v3-1
- deepseek_v3
- deepseek-ai/deepseek-v3.1

## Recorded context

- soc: A3.
- model: deepseek-ai/deepseek-v3.1, deepseek-v3-1, deepseek-v3.1, deepseek_v3, model-deepseek-v3.1.
- topology: tp8.
- execution mode: eager.
- component: model-capability, profiling-preflight.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-deepseek-v3.1; first observed: 2026-09-03.
