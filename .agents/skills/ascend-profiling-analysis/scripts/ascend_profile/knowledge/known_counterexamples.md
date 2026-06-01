# Known Counterexamples

## GLM5 MLA + sparse attention + MTP

- Capture: `D:\profiling\test\8K-1K-W8A8-TP8-MTP3-1BS`
- Shape: GLM5, TP8, W8A8, main model has 78 layers, MTP is enabled with `mtp=3`.
- Symptoms before fix:
  - `segment.py` treated `MlaPrologV3`, `KvQuantSparseFlashAttention`, and the MLA/SFA V-up projection as three separate `LayerObservation` entries. The report then showed `Layer inventory [3]` and 320 complete step segments per rank, which confused one model layer's internal attention subunits with model layers.
  - After fixing the MLA anchor frequency, `exact_regime_split` still cut each main window into a 3-layer dense prefix and a 75-layer MoE suffix. That produced fake 3-layer main steps and 75-layer main steps instead of one 78-layer GLM5 main body.
- Required behavior: when a rank has MLA layer-start anchors (`attention.mla` / `attention.mla.kv_norm_rope_cache`), those anchors define model-layer frequency. Sparse/flash score, lightning indexer, RoPE, and V-up projection events remain evidence inside that same layer window; they must not create additional layer boundaries. A short dense main-layer prefix followed by a MoE suffix with the same attention body is a model-layer family transition, not a step/workload boundary.
- Regression invariant: for this profile, the segmenter should recover four complete forward windows per rank, each with 78 main layers plus 3 MTP/speculative layers. The rank-level layer inventory should be `[78]`, not `[3]` or `[3, 75]`.
