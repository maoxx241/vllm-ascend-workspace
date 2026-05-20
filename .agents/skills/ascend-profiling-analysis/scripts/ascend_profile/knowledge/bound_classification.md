# Bound Classification for AIC/AIV Operators

This file is the single source of truth for how the analysis pipeline
labels an operator as cube-bound / vector-bound / MTE-bound / etc. It
applies to per-operator aggregates in `operator_summary.csv` and to any
report that cites those aggregates. Per-step / per-layer aggregates use
the same rules with their own filters.

## 1. Inputs

The classifier consumes:

- The 11 raw stage fields documented in `pipeline_taxonomy.md`.
- The `op_type` derived from `Accelerator Core` (see
  `pipeline_taxonomy.md` §4) — this is the **primary routing input**.
- Two legacy flags (`is_aicpu`, `is_communication`) that any caller can
  still set explicitly; they are kept so older callers keep working.

If `op_type` is missing or `unknown` and the pipeline aggregate carries
no stage signal, the result is `unknown`. We never fabricate zeros.

## 2. Output schema

The classifier emits four labels:

| Field | Range |
|---|---|
| `bound_stage` | The dominant stage. For `mix_cv` and pure AIC/AIV ops it is one of the 9 sub-stages (e.g. `aic_mac_time`, `aiv_mte2_time`). For `mix_comm_aiv` ops only the AIV stages are considered. Otherwise one of `aicpu` / `communication` / `dsa` / `unknown`. |
| `bound_family` | Coarser bucket in `{cube, vector, aic_mte, aiv_mte, scalar, mixed, aicpu, communication, comm_aiv_mix, dsa, unknown}`. |
| `dominant_core` | `aic` / `aiv` / `mix` / `none`, derived from `Σ AIC stages` vs `Σ AIV stages`. |
| `op_type` | Echoed back: `aic` / `aiv` / `mix_cv` / `mix_comm_aiv` / `communication` / `aicpu` / `dsa` / `unknown`. |

## 3. Algorithm

```text
1. If op_type == "aicpu" (or is_aicpu)   → bound_family = "aicpu",         dominant_core = "none".
2. If op_type == "dsa"                   → bound_family = "dsa",           dominant_core = "none".
3. If op_type == "communication"
       (or is_communication and op_type != "mix_comm_aiv")
                                         → bound_family = "communication", dominant_core = "none".
4. If op_type == "mix_comm_aiv":
       aiv_us = {stage: aggregate[stage] for stage in 4 AIV sub-stages}
       if Σ aiv_us == 0:
           bound_stage = "communication"; bound_family = "comm_aiv_mix"; dominant_core = "none".
       else:
           bound_stage = argmax(aiv_us); bound_family = "comm_aiv_mix"; dominant_core = "aiv".
5. Otherwise (op_type ∈ {aic, aiv, mix_cv, unknown} with pipeline signal):
   stage_us = {stage: aggregate[stage] for stage in 9 sub-stages}
   total = Σ stage_us
   if total <= 0  → bound_stage = bound_family = "unknown".
   bound_stage = argmax(stage_us)
   family_total = group stage_us by family (see pipeline_taxonomy §3)
   sort families by total descending; let top, runner be the first two.
   if (top_value - runner_value) / total < mixed_margin (default 0.10):
       bound_family = "mixed"
   else:
       bound_family = top_family
   aic_total = Σ AIC stages, aiv_total = Σ AIV stages
   determine dominant_core by absolute and relative comparison
   (mix when |aic - aiv| / max < mixed_margin).
```

`mixed_margin` is configurable; the default 0.10 is conservative. We
chose stage-then-family rather than ratio thresholds against `total`
(compute / MTE) because:

- Ratio thresholds collapse Cube and Vector into a single "compute"
  axis, hiding cube-vs-vector tradeoffs that matter for MoE / GEMM
  tuning.
- Stage-level argmax is reproducible without thresholds, while the
  family layer remains optional and explicit.

## 4. Reading the labels

- `bound_stage = aic_mac_time` → matmul (cube) is the dominant cost.
  Tuning levers: tile shape, K alignment, dtype, mac ratio.
- `bound_stage = aic_mte2_time` → AIC is starved waiting on weights /
  tensors arriving at L0A/L0B. Levers: shape padding, prefetch, L1 reuse.
- `bound_stage = aiv_mte2_time` → vector unit is GM-bound on input
  arrival to UB. Levers: layout, GM bandwidth, broadcast/reduce shapes.
- `bound_stage = aiv_mte3_time` → vector unit is GM-bound on output
  writeback (UB → GM). Levers: store coalescing, dtype, fusion to keep
  data in UB.
- `bound_stage = aiv_vec_time` → vector ALU saturates. Levers: avoid
  scalar fallback, use intrinsics, increase vectorization.
- `bound_stage = aic_scalar_time` / `aiv_scalar_time` → too many scalar
  instructions per element, often a sign of poor codegen / unrolled
  reductions.
- `bound_family = mixed` → no single family dominates. Often appears for
  fused ops (e.g. layernorm+linear) and is informative on its own; treat
  it as a hint to drill into the per-stage values rather than a defect.
- `bound_family = comm_aiv_mix` → the op is a comm-fused AIV kernel
  (`DispatchFFNCombine` and friends). The interesting question is "how
  much AIV work is overlapped with the collective". The `bound_stage`
  field tells you which AIV stage dominates that overlap.
- `op_type = mix_cv` → Cube and Vector are running concurrently. Look at
  per-op `aicore_time` vs `aiv_time` totals: if one of them is 0 in this
  CSV but the kernel is structurally CV-mixed (e.g. FIA), it means the
  CANN profiler in this run did not export the AIV side; do not infer
  that the AIV side is idle.

## 5. Limits

- Classification only ranks stages relative to each other within an
  aggregate. It does **not** compare an aggregate's bound family against
  other aggregates' families.
- Per-operator labels do not project up to per-step automatically. The
  Step / Layer / Block views recompute the family using their own filtered
  pipeline aggregate so a heavy outlier op does not mislabel the whole
  step.
- The classifier never reasons about overlap between AIC and AIV running
  in parallel: such overlap shows up indirectly through `aicore_time` /
  `aiv_time` totals vs the sum of stages, but the bound rule only ranks
  stage-level workload, not idle time.
