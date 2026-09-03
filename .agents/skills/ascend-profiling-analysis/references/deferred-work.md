# Deferred follow-ups

Tracked here so we don't lose them after PR #49 lands. None of these
block correctness; they are all "structural / maintainability" items
the reviewer flagged as P2.

## 1. `html_report.py` modularization

The file is currently ~3 k lines mixing data loading, metrics,
view-model construction, CSS, and JS. Recommended split (see review §6.4):

```
ascend_profile/html_report/
  __init__.py     # re-exports build_html_report for callers
  data.py         # Bundle, Event, _attach_raw_rows
  metrics.py     # short_op_name, union_duration_us, kernel_rollup_by_bound
  styles.py       # CSS + JS template strings
  views.py        # render_l1_view / render_l2_views / render_l3_views
  renderer.py    # render_head / render_foot / build_html_report
```

Risk: CSS is composed via f-string today; moving it requires careful
brace-escaping. Defer until we touch the views again.

**Partial progress (Phase 3):** the data layer is unified — `_load_events`
delegates to `metrics.load_events_csv` and adapts into the render-side
`Event` view (`Event.from_normalized`); `load_csv` / `load_json` delegate to
`store.csv_rows` / `store.read_json`. What remains is the view/CSS split
above.

## 2. `common.py` split — DONE (Phase 3)

`common.py` was split by responsibility; it remains as a
backwards-compatible facade that re-exports the full historic public
surface (new code should import from the real modules):

```
ascend_profile/
  models.py     # schema dataclasses (SourceRef, NormalizedEvent, segments, …)
  store.py      # JSON/JSONL/CSV/XLSX IO, id/time, coercion helpers, KNOWLEDGE_DIR
  sources.py    # rank-dir discovery + kernel_details row extraction
  pipeline.py   # CANN pipeline-stage metrics, op_type, bound_class
  work.py       # FLOP/byte estimates from shape fields
  rules.py      # categories_and_roles (YAML-driven), attention-family resolver
  metrics.py    # interval union / bubbles, quantile, artifact loaders
  common.py     # facade re-exporting the above
```

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

The old `schemas/analysis_bundle.schema.json` draft was deleted (zero
consumers — nothing loaded or validated it). A future registry would start
from the per-stage `*_manifest.json` payloads and
`knowledge/semantic_conventions.yaml`, not from that draft.

## 5. Taxonomy externalization — DONE (Phase 3)

`categories_and_roles()` is data-driven: the ordered rule list lives in
`knowledge/kernel_signatures.yaml:match_rules` and is loaded (and
schema-validated) at runtime by `rules.py`. The attention-family resolver
similarly loads `knowledge/attention_families.yaml:cheat_sheet.resolver`.
Adding operator families (new attention kernels, new MoE primitives) is a
YAML edit.

`scripts/ascend_profile/knowledge/semantic_conventions.yaml` pins the enum
catalogue (`op_type`, `op_roles`, `op_categories`, `bound_family`,
`block_kind`, `finding_type`, `anomaly_tag`, `dominant_idle_pattern`,
`soft_root_cause_label`, `alignment_method`, `alignment_confidence`,
`html_status`, `report_mode`). `tests/test_semantic_conventions.py` plus
`tests/test_kernel_signatures.py` keep the emitted values and the YAML in
sync (including the full set of categories/roles the matcher can emit).

Remaining in this area: `moe_families.yaml` is still a document-level
contract (its cheat-sheet has no production consumer; only
`tests/test_moe_families.py` mirrors it), and the finding thresholds /
wording moved to `knowledge/diagnosis_rules.yaml` while the finding
*conditions* intentionally stay in `diagnostics.py`.

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
