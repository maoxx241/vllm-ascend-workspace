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

## 6. Stage resume from interrupted run

The new `--from-stage` / `--to-stage` selectors in `analyze.py` cover
forward resumes when prior outputs are intact. A richer "stage cache"
that detects stale inputs and replays only the dirty stages is the
natural next step, especially once we have schema validation in place.

## 7. `ascend-profiling-anomaly` overlap

The user-level `ascend-profiling-anomaly` skill (in `.claude/`) still
operates on raw kernel_details for ad-hoc anomaly hunts. Once this
skill stabilizes, decide whether to (a) deprecate the anomaly skill,
or (b) have it call into this skill's framework as a thin orchestrator.
