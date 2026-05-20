# Step Anatomy: head / main / tail / bubble

Every `segment_type == step` row is decomposed into four time buckets so
the report can answer "where does the step time actually go" without
running a second pass over raw data. The decomposition is computed in
`tools/ascend_profile/summarize.py:step_anatomy_rows` and persisted to
`step_anatomy.csv`. A subset of the columns is also inlined into
`step_summary.csv` for spreadsheet ergonomics.

## 1. Boundary definitions

For each step segment with `layer_count >= 1`:

```text
head  = [step.row_start, layer[0].row_start - 1]
main  = [layer[0].row_start, layer[-1].row_end]
tail  = [layer[-1].row_end + 1, step.row_end]
```

`layer[0]` and `layer[-1]` are taken from `layer_segments.json` filtered
by `segment_id == step.segment_id` and ordered by start time. The same
ordering is used for time-domain bounds (`start_us` / `end_us`).

If a step has zero layers in `layer_segments.json` (rare, e.g. extreme
edge windows that survived segmentation but contain no layer-grade
evidence), the entire step is booked as `head_only` so the report can
flag it explicitly. We never invent a synthetic layer to pad the
anatomy.

## 2. Per-window metrics

Each window emits the same four scalar metrics, derived strictly from
events whose `row_idx` falls in the window's row range:

- `<window>_wall_ms` — `end_us - start_us` of the window. Zero when the
  window is empty.
- `<window>_busy_ms` — busy union of merged event segments inside the
  window (kernel-active time).
- `<window>_bubble_ms` — `wall_ms - busy_ms` clamped at zero. This is
  the device-idle bubble inside that part of the step.
- `<window>_event_count` — number of events whose row falls in the
  closed range.

Step totals (`step_wall_ms`, `step_busy_ms`, `step_bubble_ms`) come from
the existing step_summary metrics, not from summing the three windows.
This intentionally surfaces any inconsistency between windowed evidence
and the parent step's metrics; the report prints both so the user can
verify alignment.

## 3. Ratios

`head_ratio`, `main_ratio`, `tail_ratio` are wall-time fractions of
**the step**, not of the union of windows:

```text
head_ratio = head_wall_ms / step_wall_ms
main_ratio = main_wall_ms / step_wall_ms
tail_ratio = tail_wall_ms / step_wall_ms
```

`bubble_ratio = step_bubble_ms / step_wall_ms`. These three sum to ≤ 1
(equal to 1 when the layer windows tile the step exactly, which is the
common case).

## 4. Reading the rows

- `anatomy_kind = full` — the step has at least one layer. Use the head
  / main / tail wall ratios as a first cut; a high `head_ratio` implies
  long warmup / scheduling latency, a high `tail_ratio` implies
  postprocessing / sampling overhead.
- `anatomy_kind = head_only` — the step has no detectable layer. The
  entire step is treated as head; the row stays in the table so the
  report can call it out as an unclassified slice.
- Use `head_row_*`, `main_row_*`, `tail_row_*` to jump directly to the
  underlying CSV rows for evidence; the time fields are auxiliary.

## 5. Cross-references

- Step segmentation rules: `tools/ascend_profile/segment.py` (especially
  `StepPlan` / `StepSegment.segment_type`).
- Layer segmentation rules: `LayerSegment` and the layer assembly logic
  in `segment.py:build_layers` and downstream helpers.
- Pipeline-stage interpretation per layer: `pipeline_taxonomy.md`.
