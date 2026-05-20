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
`bound_family`, `block_kind`, `finding_type`, `alignment_method`,
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

## 10. `ascend-profiling-anomaly` overlap

The user-level `ascend-profiling-anomaly` skill (in `.claude/`) still
operates on raw kernel_details for ad-hoc anomaly hunts. Once this
skill stabilizes, decide whether to (a) deprecate the anomaly skill,
or (b) have it call into this skill's framework as a thin orchestrator.
