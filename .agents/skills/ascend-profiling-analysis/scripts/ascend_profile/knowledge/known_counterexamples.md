# Known Counterexamples

## Salvaged anomaly rules (provenance note)

The edge-gap / recurrence / capture-boundary / host-attribution rules in
`summarize.py` + `host_trace.py` were salvaged from the retired user-level
`ascend-profiling-anomaly` skill (`scripts/reference_host_gap_branch.py`,
`references/rulebook.md` §10/§11/§12). Thresholds were carried over
unchanged (`>= max(1.0 ms, 10% wall)` edge gaps, `>= 60%` step recurrence,
`0.2 / 0.05 / 0.1` host-coverage boundaries, wait-anchor `0.95 / 10 us /
top-10`, AICPU `0.9 / 0.2`). Two deliberate divergences from the old
prototype, both conservative:

- The old skill's step windows came from row-midpoint tiling, so
  prelaunch/tail gaps were intra-window; this project's windows are
  event-derived, so the gaps are measured against neighbouring segments
  (exact cover makes that the same idle). Capture edges report `None`
  (unknown), never zero.
- `RECURRING_BUBBLE_PATTERN` gained a `>= 3 complete steps` minimum-vote
  guard, and `PARTIAL_CAPTURE_BOUNDARY` requires the incomplete boundary
  segment to hold at least half the median complete-step event count —
  the old rulebook tagged on any capture-edge anomaly.

## GLM5 MLA + sparse attention + MTP

- Capture: `D:\profiling\test\8K-1K-W8A8-TP8-MTP3-1BS`
- Shape: GLM5, TP8, W8A8, main model has 78 layers, MTP is enabled with `mtp=3`.
- Symptoms before fix:
  - `segment.py` treated `MlaPrologV3`, `KvQuantSparseFlashAttention`, and the MLA/SFA V-up projection as three separate `LayerObservation` entries. The report then showed `Layer inventory [3]` and 320 complete step segments per rank, which confused one model layer's internal attention subunits with model layers.
  - After fixing the MLA anchor frequency, `exact_regime_split` still cut each main window into a 3-layer dense prefix and a 75-layer MoE suffix. That produced fake 3-layer main steps and 75-layer main steps instead of one 78-layer GLM5 main body.
- Required behavior: when a rank has MLA layer-start anchors (`attention.mla` / `attention.mla.kv_norm_rope_cache`), those anchors define model-layer frequency. Sparse/flash score, lightning indexer, RoPE, and V-up projection events remain evidence inside that same layer window; they must not create additional layer boundaries. A short dense main-layer prefix followed by a MoE suffix with the same attention body is a model-layer family transition, not a step/workload boundary.
- Regression invariant: for this profile, the segmenter should recover four complete forward windows per rank, each with 78 main layers plus 3 MTP/speculative layers. The rank-level layer inventory should be `[78]`, not `[3]` or `[3, 75]`.

## Kimi-K3 16-expert pruned (period-4 KDA + gating-MLA, tail double-full)

- Capture: `/vllm-workspace/.vaws-runtime/serving/20260904_014505/vllm_profile` (k3-153, TP4, eager, 16-expert pruned K3, 2026-09-04).
- Structure ground truth: checkpoint `quant_model_weights.safetensors.index.json` has 93 weight layers; config `num_hidden_layers=93`, `full_attn_layers=[4,8,...,88,92,93]` (1-indexed, tail layers 92 AND 93 both full attention), `kda_layers` covers the rest (69 KDA + 24 MLA).
- Observed: segmentation produces 92 contiguous layers/step (median 92, 4/560 steps at 90) instead of 93; the tail unit (layer_index 91, span 191 rows vs median ~70) folds **[final KDA layer content + lm_head + ArgMax sampler + next-step slot mapping]** into one "layer". MLA-unit count is exactly right (24), one KDA unit is absorbed at the tail boundary. All 4 ranks identical (`model_guided`, no anchor degradation).
- Why it happens: the step tail region (final norm + lm_head + sampler) is not separated from the last transformer layer when no `block_head` (AddRmsNorm-style) boundary exists between them; with the tail double-full-attention exception the period-4 anchor phase also has no KDA gap to close the sequence cleanly.
- Detection that worked: the A4 layer-count invariant flags `layer_count_validation: mismatch` (expected 93, observed 90/92) and `layer_validation.status: degraded` in analysis_summary — i.e. the miss is surfaced, never silent.
- Related fix (not the root fix): layer-count-invariant retry now skips small deltas (`<= max(2, 5%)`) — the persistent off-by-1 here caused an 8x resegmentation retry storm (2380s -> ~300s segment). Root fix (separating step tail from the last layer without norm boundaries) is deferred to the segmentation golden-fixture round; do NOT patch `segment.py` blind for it.
- Fingerprint note: K3-style profiles (linear + moe.gating + mla present) must not resolve to `Qwen3.5 family`; the family's operator_match now forbids `attention.mla` (2026-09-04). K3 wins by elimination when linear-attention and MLA kernels coexist.
