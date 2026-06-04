# Model Knowledge TODO

The segmenter now consumes catalog-backed model context before statistical
fallbacks.  Unknown or unverified model facts must be collected here first, then
moved into `model_fingerprints.json` only after a profile-backed validation.

## Profile-Visible Main Body Lengths

- DeepSeek-V4: Flash and Pro public configs are registered separately.  Collect
  separate `profile_visible_layer_counts` for non-flash, prefill, decode, and
  any mixed CSA/HCA runtime mode.  Current validated Flash hint is 43.
- Qwen3.5-397B-A17B: validate whether the observed 40/41 visible main layers
  are stable across prefill/decode, TP/EP layouts, and graph/eager modes.
- DeepSeek-V2-Lite / DeepSeek-V2 / DeepSeek-V3: collect mode-specific visible
  body lengths and whether MLA markers are always the primary layer-start
  anchor.
- GLM5, Qwen3-Next, and other hybrid attention/MoE models: collect visible
  main body lengths by profile mode before enabling model-guided segmentation.

## Public Config Resolution

- For every known model family with missing `expected_layers`, add concrete
  `variants` entries and verify each variant through Hugging Face or ModelScope
  `config.json` before using it in segmentation.
- If a user provides a fuzzy family name, enumerate all registered variants and
  match each one.  Do not infer layers from the family name itself.
- Treat quantization/data-format suffixes such as BF16, FP8, INT8, W4A8, AWQ,
  GPTQ, GGUF, NVFP4, and MTP packaging as non-structural unless a separate
  config proves changed layers or architecture.

## Speculative Decoding Templates

- Collect MTP/Eagle visible layer templates per model, including whether the
  speculative body carries attention, MoE-only, dense-only, or sampling/AICPU
  markers.
- Record the row-boundary markers that separate main body, speculative tail,
  logits/lm_head, and sampling windows.
- Add explicit speculative-body catalog hints only after the template is stable
  across at least two captures; until then, shorter bodies stay
  `partial_body_window`.

## Operator And Phase Templates

- For each known model, collect attention phase operator templates, MoE
  dispatch/expert/combine templates, dense FFN templates, lm_head/logits
  templates, and sampling/AICPU templates.
- Promote concrete DSA / CSA / HCA model variants only after collecting public
  configs or validated profile-visible layer hints.  Current operator fast
  paths can identify generic DSA/DSV4/Qwen3.5-style families, but single weak
  signals such as MoE gating alone must remain generic.
- Capture mode labels that affect templates: flash vs non-flash attention,
  graph vs eager, prefill vs decode, CANN version, vLLM commit, and
  vLLM-Ascend commit.

## Parallel Strategy And Rank Roles

- Collect TP/EP/DP/PP layout, rank role, dummy/runner rank markers, and
  request/microbatch identifiers from collection manifests when available.
- Record whether a single-rank profile is a shard-only view; do not promote
  shard-local parameter or layer counts to global model facts without layout
  evidence.

## Hardware Context

- Store capture-hardware model, CANN platform config, and sustained-factor
  evidence alongside analysis runs when the profiling root is historical.
- Keep MFU denominators tied to theoretical peak and roofline expectations tied
  to sustained peak so report semantics remain explicit.
