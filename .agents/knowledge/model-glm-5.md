# GLM-5: DSA sparse-attention MoE (same structure family as DeepSeek-V3.2: lightning indexer + sparse shared-KV, no KV compressor); layer count config-driven

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-03. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Recorded model structure

User-confirmed 2026-09: GLM-5 shares the DSA (DeepSeek-V3.2) sparse-attention structure. expected_layers stays null on purpose: read num_hidden_layers from the model's config.json instead of guessing; replace with a verified count once a GLM-5 profile is collected.

## Profiling observation

Recorded model structure from workspace profiling validation: expected_layers=78

## Usage context

Use the recorded structure when planning collection/analysis preflight.

## Search terms

- glm-5
- glm5
- glm5forcausallm

## Recorded context

- model: glm-5, glm5, glm5forcausallm, model-glm-5.
- component: model-capability, profiling-preflight.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-glm-5; first observed: 2026-09-03.
