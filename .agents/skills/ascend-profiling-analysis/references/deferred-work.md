# Deferred follow-ups

Tracked here so we don't lose them after PR #49 lands. None of these
block correctness; they are all "structural / maintainability" items
the reviewer flagged as P2.

## 1. `html_report.py` modularization

The file is currently ~2.7 k lines mixing data loading, metrics,
view-model construction, CSS, and JS. Recommended split (see review §6.4):

```
ascend_profile/html_report/
  __init__.py     # re-exports build_html_report for callers
  data.py         # Bundle, Event, _load_events, _attach_raw_rows
  metrics.py     # short_op_name, union_duration_us, kernel_rollup_by_bound
  styles.py       # CSS + JS template strings
  views.py        # render_l1_view / render_l2_views / render_l3_views
  renderer.py    # render_head / render_foot / build_html_report
```

Risk: CSS is composed via f-string today; moving it requires careful
brace-escaping. Defer until we touch the views again.

## 2. `common.py` split

`ascend_profile/common.py` is ~1.4 k lines containing schema dataclasses,
CSV/JSON IO, rank discovery, shape parsing, taxonomy/role classification,
pipeline-bound classification, metrics/interval-union, and XLSX writing.

Suggested split:

```
ascend_profile/
  schema.py     # SCHEMA_VERSION + dataclasses + EvidenceRef
  io.py         # csv_rows, write_csv, read_json, write_json
  taxonomy.py   # categories_and_roles, role classification
  pipeline.py   # pipeline_bound, mte/aic ratios
  metrics.py    # interval union, percentile helpers
  xlsx.py       # write_xlsx
```

Risk: every stage imports from `common`. Needs a deprecation shim or
a deep refactor commit. Defer until the schema is otherwise stable.

## 3. `segment.py` split

The segmentation module is ~2.7 k lines and has the highest correctness
impact in the framework. Suggested package layout:

```
ascend_profile/segment/
  anchors.py        # role / anchor extraction
  layers.py         # layer observations
  frames.py         # frame / step plan composition
  validators.py     # exact-cover / residual / composite-body validation
  materialize.py    # StepSegment / LayerSegment / EvidenceRef writeout
  __init__.py
```

Risk: this is the single most error-prone module. Defer until we have
golden-output regression tests across our reference profiling cases.

## 4. JSON schema registry

Today, each stage writes its own `*_manifest.json` with a stage-local
shape; the skill launcher reads scalar fields out of them. A
`schemas/*.schema.json` registry plus JSON-schema validation would
let us:

* fail fast on stage-output drift
* document the artifact surface in one place
* power IDE auto-complete for downstream consumers

Already have `schemas/analysis_bundle.schema.json` as a starting point.

## 5. Taxonomy externalization

`categories_and_roles()` is currently Python code that pattern-matches
kernel names. A `taxonomy.yaml` rule file plus a tiny matcher would let
us add operator families (new attention kernels, new MoE primitives)
without editing Python. See review §6.5 for the proposed shape.

**Partial progress:** `scripts/ascend_profile/knowledge/semantic_conventions.yaml`
now pins the enum catalogue (`op_type`, `op_roles`, `op_categories`,
`bound_family`, `block_kind`, `finding_type`, `anomaly_tag`,
`dominant_idle_pattern`, `soft_root_cause_label`, `alignment_method`,
`alignment_confidence`, `html_status`, `report_mode`).
`tests/test_semantic_conventions.py` keeps Python and YAML in sync. The
next step is replacing the Python rule body of `categories_and_roles()`
with a YAML-driven matcher (`operator_taxonomy.yaml`).

## 5b. Segmentation strategy externalization

`segment.py` is the most safety-critical module in the framework; we
have not externalized its rules. The follow-up PR should add
`knowledge/segmentation_strategy.yaml` with these parameter blocks:

* `anchor_priority` (role / category ordering)
* `boundary_markers` (block_head, normalization, selection)
* `residual_policy` (head/tail allow vs hard_fail, interior policies)
* `repair_rules` (toggleable rule names, no algorithm changes)

Acceptance for that follow-up: golden segmentation fixtures must keep
passing (see §8).

## 6. Stage resume from interrupted run

The new `--from-stage` / `--to-stage` selectors in `analyze.py` cover
forward resumes when prior outputs are intact. A richer "stage cache"
that detects stale inputs and replays only the dirty stages is the
natural next step, especially once we have schema validation in place.

## 7. Golden segmentation fixtures

Recommended layout (see review §3.8):

```
tests/fixtures/segmentation/
  qwen_moe_tp4_minimal/
    kernel_details_rank0.csv
    expected_step_segments.json
    expected_layer_segments.json
  argmax_not_boundary/
    kernel_details.csv
    expected_no_standalone_step_boundary.json
  companion_layer/
    kernel_details.csv
    expected_companion_layer.json
```

Tests: `test_operator_taxonomy_rules.py`,
`test_segmentation_strategy_rules.py`,
`test_known_counterexamples.py`,
`test_stage_resume_contract.py`.

This is the prerequisite for landing §5b (segmentation strategy YAML)
safely.

## 8. UI-only heuristic → diagnostic findings

`compute_ep_balance`, `assess_companion_run`, `detect_attention_subtype`,
`derive_layer_composition`, `guess_model_structure` are flagged
`UI-only` in the HTML (see ribbon on the L1 KPI strip and the Composition
column header in L2). Promoting them into `diagnostics.py` proper means
emitting them as `ep_load_imbalance_suspected` /
`reduced_work_or_dummy_rank` / `rank_workload_asymmetry` findings with
real `evidence_ids` plus a non-empty `limitations` string when the
heuristic is necessarily soft. Deferred because it requires alignment
work in `cross_rank.py` first (a finding-grade EP imbalance claim needs
per-step alignment, not just a per-rank wall-time aggregate).

## 9. `--remote-output-dir` semantics

Wrapper now accepts `--remote-output-dir <abs>` for partial reruns.
Open follow-ups:

* When the user reuses a remote output dir but the local run dir is new,
  we still tar-sync the framework — that's fine, but we could skip the
  sync if `<framework>/.version` matches the local checkout.
* The wrapper does not yet verify that the remote dir belongs to the
  same `remote_profile_root`. A small sanity check (read remote
  `manifest.json:input.root`, compare to current `--remote-profile-root`)
  would catch foot-guns.

## 10. `segment.py` exact-cover performance

`validate_unresolved_composite_bodies` calls
`sequence_occurrence_count(sequence, template)` for every plan/template
pair. The inner loop is brute-force `O(n·m·|template|)` substring
matching. On a dsv4 prefill rank (~3k complete plans × ~50 recurring
templates × avg seq_len ~15) this is ~10-30 s, and the surrounding
`exact_cover_sequence` memoized DP adds another large constant. The
8-minute dsv4 prefill segment stage observed during the May 2026 sweep
is plausibly dominated by this code path.

A KMP-based occurrence counter (`O(n+m)` per pair) or pre-indexed
template-anchor table (`O(n)` per plan, amortized) would remove the
hot spot without changing semantics. Defer this until segmentation
strategy externalization (§5b) lands, so the perf work and the rule
restructuring touch `segment.py` together.

Acceptance: dsv4 prefill segment stage finishes in < 60 s; existing
unit tests in `tests/test_segment_validator.py` continue to pass.

## 11. `ascend-profiling-anomaly` overlap — RESOLVED (deprecate)

The user-level `ascend-profiling-anomaly` skill (in `.claude/`) still
operates on raw kernel_details for ad-hoc anomaly hunts. Once this
skill stabilizes, decide whether to (a) deprecate the anomaly skill,
or (b) have it call into this skill's framework as a thin orchestrator.

**Resolution (profiling-skills refactor, Phase 2):** option (a). The
anomaly skill's unique detection capabilities were salvaged into this
framework with thresholds unchanged (its rulebook §10/§11/§12):

- `PRELAUNCH_GAP_HEAVY` / `TAIL_GAP_HEAVY` step tags and
  `prelaunch_gap_ms` / `tail_gap_ms` columns (`summarize.neighbor_gap_map`,
  `summarize.anomaly_tags`);
- rank-level `RECURRING_BUBBLE_PATTERN` rollup + `dominant_idle_pattern`
  (`summarize.recurring_bubble_rollup`, `diagnostics.diagnose_recurring_bubbles`);
- conservative `PARTIAL_CAPTURE_BOUNDARY`
  (`summarize.apply_partial_capture_boundary_tags`);
- host-side bubble soft attribution from `trace_view.json`
  (`host_trace.py`, `evidence/bubble_windows.jsonl:soft_attribution`).

Already covered before the salvage (verified identical thresholds):
bubble detection (`common.bubble_windows`, `DEVICE_IDLE_GAP_HEAVY` /
`INTERNAL_BUBBLE_HEAVY`), wait-anchor false hotspots
(`0.95 / 10 us / top-10`), AICPU exposure (`0.9 / 0.2`). The anomaly
skill's Mode-1 orchestration (collect + analyze) is owned by
`ascend-profiling-collection`; its `--machine` entry is deprecated.
The old skill keeps a DEPRECATED banner pointing here and is no longer
maintained.
