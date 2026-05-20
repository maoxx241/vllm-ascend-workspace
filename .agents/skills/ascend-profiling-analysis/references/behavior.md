# Profiling Analysis Skill Behavior

## Lifecycle

1. **Resolve machine** from local inventory (alias or IP).
2. **Resolve input**:
   - `--manifest <local-run-dir>/manifest.json` → produced by `ascend-profiling-collection`. We require `analysis_status == "ok"` and a non-empty `remote_profile_root`.
   - `--remote-profile-root <abs-path>` → raw remote path (used for historical roots not collected through the collection skill).
3. **Parity sync** (light): tar-over-ssh only `scripts/ascend_profile/` from the local skill dir to `<remote-work-dir>/ascend_profile/`. Excludes `__pycache__` and `*.pyc`. Does **not** touch `.vaws-runtime/` or sync the entire repo.
4. **Remote analyze**: run `python3 -m ascend_profile.analyze <ROOT> --output <OUT> --verbose` from inside `<remote-work-dir>`. stdout/stderr is streamed back so the agent can see stage timings live.
5. **Validate artifacts**: every required artifact must exist, and `segment_manifest.json` must have `hard_errors == 0` and `interior_island_total == 0`.
6. **Pull artifacts**: lightweight by default (`report/`, `*_manifest.json`, `diagnosis_findings.json`, summary CSVs, `step_segments.json`, `layer_segments.json`, `structure_evidence_graph.json`, `evidence_index.csv`, `raw_kernel_index.csv`). Use `--keep-remote-output` to mirror the entire remote output dir locally.
7. **Emit JSON** on stdout. Progress lines (`__VAWS_PROFILE_ANALYSIS_PROGRESS__=...`) go to stderr.

## Required artifacts (single-root `analyze`)

These must exist in the remote output dir before the skill declares success:

```
manifest.json
segment_manifest.json
diagnosis_findings.json
report/report.md
report/report.xlsx
report/report.html
```

The HTML report is best-effort: if rendering hits an exception the analyze stage
still succeeds, a stub `report.html` with the error message is written, and
`report/manifest.json:html_status` is set to `error`. Callers should check that
field before assuming the rich HTML view is available.

Lightweight pull set (always pulled when `--keep-remote-output` is not set):

```
manifest.json
normalize_manifest.json
segment_manifest.json
classify_manifest.json
summary_manifest.json
cross_rank_manifest.json
diagnosis_findings.json
evidence_index.csv
raw_kernel_index.csv
rank_summary.csv
step_summary.csv
step_anatomy.csv
step_class_summary.csv
layer_summary.csv
layer_class_summary.csv
block_summary.csv
block_class_summary.csv
operator_summary.csv
operator_class_summary.csv
hccl_op_summary.csv
hccl_class_summary.csv
wait_anchor_ops.csv
aicpu_summary.csv
cross_rank_alignment.csv
cross_rank_alignment.json
step_segments.json
layer_segments.json
block_segments.json
class_signatures.json
structure_evidence_graph.json
report/manifest.json
report/report.md
report/report.xlsx
report/report.html
```

Excluded from the lightweight set (large or only useful for deep debug; pull on demand with `--keep-remote-output` or by explicit `scp`):

```
normalized_event_index.csv
normalized_event_index.jsonl
evidence/bubble_windows.jsonl
```

## Local run directory layout

```
.vaws-local/profiling-analysis/runs/<timestamp>_<tag>/
  skill_run.json                   # this skill's run metadata
  collection_manifest.json         # copy of the input collection manifest (if --manifest)
  manifest.json                    # mirror of remote manifest.json
  segment_manifest.json
  diagnosis_findings.json
  ...                              # other lightweight pull artifacts
  report/
    report.md
    report.xlsx
    manifest.json
  sweep_summary.json               # only for profile_sweep.py
  sweep_class_rollup.csv           # only for profile_sweep.py (multi-root rollup)
```

## Remote work directory layout

```
<remote-work-dir>/                  # default /tmp/ascend_profile_framework
  ascend_profile/                   # tar-synced from local skill dir
  runs/<timestamp>_<tag>/           # single-root analyze output
  sweeps/<timestamp>_<tag>/         # multi-root sweep output
```

The skill never mutates anything outside `<remote-work-dir>` and the user-provided profiling roots.

## Configuration priority

| Source | Role |
|--------|------|
| `--manifest` | Authoritative for `remote_profile_root` and `analysis_status`. The skill refuses to run when the manifest reports anything other than `analysis_status == "ok"`. |
| `--remote-profile-root` | Used only when `--manifest` is not supplied. The agent is responsible for confirming the path is correct. |
| `--remote-work-dir` | Optional override; default `/tmp/ascend_profile_framework`. |
| `--keep-remote-output` | Pull every file back, instead of the lightweight subset. Use only when you actually need `normalized_event_index.csv` or bubble window evidence. |
| `--remote-timeout` | Hard wall-clock cap for the remote command. Single-root default 3600s; sweep default 14400s (matches the published 61-root regression baseline). |

## Failure policy

Hard fail (`status: "failed"` in stdout JSON, non-zero exit code):

| Phase | Cause | exit code |
|-------|-------|-----------|
| `manifest_validation` | manifest missing / malformed / `analysis_status != "ok"` / `remote_profile_root` empty | 2 |
| `parity_sync` | tar-sync of `scripts/ascend_profile/` to remote failed | 3 |
| `remote_analyze` | remote `analyze.py` exited non-zero or hit `--remote-timeout` | 4 |
| `artifact_validation` | any required artifact missing, or `segment_manifest.json` reports `hard_errors > 0` / `interior_island_total > 0` | 5 |
| `artifact_pull` | rsync of artifacts back to the local run dir failed | 6 |

Soft outcomes (still `status: "ok"`):

- Diagnosis findings with `confidence: "low"` are reported as-is. The skill does not silently downgrade or drop them.
- A finding with `confidence: "medium"` and one missing corroborating source is acceptable.
- Cross-rank asymmetries without business context (could be VIT, dummy run, encoder, decode-only, etc.) stay as `rank_workload_asymmetry` without naming a model component.

## Sweep behavior

`profile_sweep.py` is a thin wrapper around `ascend_profile.sweep`. It:

- Calls the remote sweep with all `--search-root`s the agent provides.
- Pulls back `sweep_summary.json` and `sweep_class_rollup.csv` (the multi-root rollup table) plus every successful root's `report/` and `*_manifest.json`. Use `--pull-html` to additionally fetch per-root `report/report.html` files.
- Reports a layer inventory in the form `{"(27, 40)": 17, "(24,)": 9, ...}` so the agent can cross-compare captures.
- Returns `status: "partial"` (exit code 1) when any root failed but the summary was still produced. `status: "failed"` (exit codes 3-6) is reserved for setup / pull failures that prevent the summary from being written at all.

## Evidence chain (mandatory for agent answers)

When the agent reports findings to the user, every claim must be traceable through:

```
report claim
  → diagnosis finding (diagnosis_findings.json)
  → evidence id (evidence_index.csv / structure_evidence_graph.json)
  → event / segment / alignment id
  → source path + row range (raw_kernel_index.csv)
  → original kernel_details.csv / trace_view.json / op_summary / communication.json
```

If a claim cannot be backed at row level, the agent must surface it as a `limitation`, not a conclusion.

## What this skill does NOT do

- Start or stop services. Use `vllm-ascend-serving`.
- Run benchmarks. Use `vllm-ascend-benchmark`.
- Collect new torch profiler data (drive `/start_profile` / `/stop_profile`, run `analyse()`). Use `ascend-profiling-collection`.
- Attribute HBM / 显存. Use `ascend-memory-profiling`.
- Edit submodule code or push commits.
- Rewrite single-rank step boundaries from cross-rank evidence (the analysis framework intentionally forbids this).
