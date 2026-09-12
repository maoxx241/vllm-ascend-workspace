# DeepSeek-V2-Lite: 27-layer MLA + MoE model; verified TP1 on A3

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-03. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Recorded model structure

Standard 27-layer MLA+MoE body (hidden 2048, 16 attention heads, 64 experts). MTP/Eagle-style short speculative bodies must not enter the step layer inventory.

## Profiling observation

Recorded model structure from workspace profiling validation: expected_layers=27; attention_family=mla, moe, moe, mla

## Usage context

Use the recorded structure when planning collection/analysis preflight. Verified configs: tp1/enforce_eager/A3.

## Search terms

- deepseek-v2-lite
- dsv2-lite
- dsv2lite
- deepseekv2liteforcausallm

## Recorded context

- soc: A3.
- model: deepseek-v2-lite, deepseekv2liteforcausallm, dsv2-lite, dsv2lite, model-deepseek-v2-lite.
- topology: tp1.
- execution mode: eager.
- component: model-capability, profiling-preflight.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: model-deepseek-v2-lite; first observed: 2026-09-03.
