# Ascend NPU Pipeline Taxonomy (Atlas A2 / A3)

This file is the contract every analysis stage in this repo uses when it
talks about "pipeline time", "compute vs MTE", or "Cube vs Vector". It
intentionally mirrors the CANN profiling field names so every aggregate
metric stays one-to-one with raw evidence in `kernel_details.csv`.

Sources: CANN community edition msprof reference (CANN ≥ 7.0); MindStudio
profiler "AICore Performance" panel; Atlas A2/A3 architecture spec.

## 1. Architecture: decoupled Cube and Vector

Atlas A2/A3 NPUs ship a **decoupled** AI Core (cube) and AI Vector unit:

- **AI Core (AIC)** runs the cube/matmul pipeline. Its sub-stages are
  `mac` (cube matmul), `fixpipe` (writeback / quantization fix-up),
  `mte1` (L1 → L0A/L0B), `mte2` (GM/L1 → L0A/L0B), and `scalar` (AIC
  scalar instructions). Total wall time exposed as `aicore_time(us)`.
- **AI Vector (AIV)** runs the vector / element-wise / DMA pipeline. Its
  sub-stages are `vec` (vector ALU), `mte2` (GM → UB), `mte3` (UB → GM),
  and `scalar` (AIV scalar instructions). Total wall time exposed as
  `aiv_time(us)`.

Because the two pipelines are decoupled, `aic_mte2_time` and
`aiv_mte2_time` describe **different** memory paths and **must stay
separate**. Merging them masks the actual bottleneck (e.g. an op that
suffers GM→UB pressure from AIV will be misdiagnosed as Cube-side mte2
when the values are summed).

## 2. Stage-to-column mapping (kernel_details.csv)

| Pipeline | Stage | CSV column (with unit) | Field key in this repo |
|---|---|---|---|
| AIC | matmul cube | `aic_mac_time(us)` | `aic_mac_time` |
| AIC | writeback / fixpipe | `aic_fixpipe_time(us)` | `aic_fixpipe_time` |
| AIC | mte1 (L1 → L0) | `aic_mte1_time(us)` | `aic_mte1_time` |
| AIC | mte2 (GM/L1 → L0A/B) | `aic_mte2_time(us)` | `aic_mte2_time` |
| AIC | scalar | `aic_scalar_time(us)` | `aic_scalar_time` |
| AIV | vector ALU | `aiv_vec_time(us)` | `aiv_vec_time` |
| AIV | mte2 (GM → UB) | `aiv_mte2_time(us)` | `aiv_mte2_time` |
| AIV | mte3 (UB → GM) | `aiv_mte3_time(us)` | `aiv_mte3_time` |
| AIV | scalar | `aiv_scalar_time(us)` | `aiv_scalar_time` |
| AIC | total wall | `aicore_time(us)` | `aicore_time` |
| AIV | total wall | `aiv_time(us)` | `aiv_time` |

These 11 columns are stored unchanged into `pipeline_us` per event in
`normalized_event_index.csv` and aggregated by op in
`operator_summary.csv`. We never invent new column names; downstream
stages must always be able to point to the original CSV cell.

Field shape inside `pipeline_us`: a JSON object with **exactly** the
keys above (or empty if the source kernel_details.csv did not expose any
of them — older CANN releases). Empty dict means *no signal*; do not
fabricate zeros.

## 3. Compute vs MTE vs Scalar grouping

For coarser bound classification we group stages into families:

- `cube` family ← `aic_mac_time + aic_fixpipe_time`
- `vector` family ← `aiv_vec_time`
- `aic_mte` family ← `aic_mte1_time + aic_mte2_time`
- `aiv_mte` family ← `aiv_mte2_time + aiv_mte3_time`
- `scalar` family ← `aic_scalar_time + aiv_scalar_time`

Note: `aic_mte` and `aiv_mte` stay separate by design (see § 1). When the
report says "MTE bound", it must qualify which side.

## 4. op_type taxonomy

Every event also carries a coarse `op_type` derived from the
`Accelerator Core` column in `kernel_details.csv`. This is the source of
truth for "what kind of kernel is this", independent of pipeline signal:

| op_type | Trigger | Meaning |
|---|---|---|
| `aic` | core = `AI_CORE` / `AICORE` | Pure cube unit kernel. AIV stages should all be zero. |
| `aiv` | core = `AI_VECTOR_CORE` / `AIVECTOR` | Pure vector unit kernel. AIC stages should all be zero. |
| `mix_cv` | core = `MIX_AIC` / `MIX_AIV` (any mix variant) | **Cube + Vector run simultaneously**, e.g. FlashAttention / FusedInferAttentionScore / GroupedMatmul. Both `aicore_time` and `aiv_time` may carry signal; the AIC and AIV stage columns describe the work each side did. **Never collapse this into pure AIC or pure AIV.** |
| `mix_comm_aiv` | core = `COMMUNICATION` AND `aiv_time > 0` | Comm + AIV fused kernel, e.g. `DispatchFFNCombine`, `MoeDistributeDispatch`, `MoeDistributeCombine`. The AIC side is irrelevant; only AIV stages describe the compute work overlapped with the collective. |
| `communication` | core = `COMMUNICATION` AND `aiv_time == 0` | Pure HCCL collective; no pipeline figures. |
| `aicpu` | core = `AI_CPU` / `AICPU` | Host-side kernel; pipeline columns intentionally empty. |
| `dsa` | core = `DSA_SQE` | Driver Service Agent / queue side-band; not real compute. |
| `unknown` | Anything else | Treat as no signal. |

The `mix_comm_aiv` rule is intentional: when CANN reports a fused
collective like `DispatchFFNCombine` it sets `Accelerator Core =
COMMUNICATION` even though half the runtime is AIV vector work. The
classifier checks for non-zero AIV stage time and re-labels so the
report can attribute the AIV burden separately.

## 5. Coverage and missing data

- Coverage is reported in `summary_manifest.json:pipeline_coverage`:
  `events_with_pipeline_signal / events_total` plus the same ratio at the
  operator-aggregate level. A 0.0 coverage means the source kernel
  details did not include AIC/AIV stage columns and **no** pipeline
  conclusion can be drawn — downstream stages must skip pipeline figures
  for that root, not fall back to heuristics.
- AICPU, pure communication, and DSA events legitimately have empty
  pipeline dicts; they are not counted as "missing data". The bound
  classifier short-circuits them to dedicated labels (`aicpu`,
  `communication`, `dsa`).
- For `mix_cv` ops, the AIC and AIV stage columns are independent
  observations: a `MIX_AIC` op can show `aiv_time = 0` on older CANN
  versions even though the kernel structurally requires AIV. We do not
  fabricate AIV stage data in that case; instead the report flags the
  per-op AIC and AIV totals side-by-side so the user can spot the
  asymmetry.

## 6. Cross-references

- Bound classification rules: see `bound_classification.md`.
- Step-level head / main / tail / bubble decomposition: see
  `step_anatomy.md`.
