# Profiling Analysis Skill Behavior

## Relationship to remote-dev

Use `.remote-dev` tools for ad hoc remote read/edit/bash/search/patch around
profiling roots and generated reports. This skill owns analysis semantics and
keeps the existing scripts as the managed VAWS compatibility backend.

## Lifecycle

1. **Resolve session** target: auto-resolved from the cwd worktree binding (`.vaws-local/current-session.json`) when no target arg is passed, or from an explicit `--session-id` / `--session-file`. A session-scoped `--manifest` supplies the session automatically.
2. **Resolve input**:
   - `--manifest <local-run-dir>/manifest.json` → produced by `ascend-profiling-collection`. We require `analysis_status == "ok"` and a non-empty `remote_profile_root`.
   - `--remote-profile-root <abs-path>` → raw remote path (used for historical roots not collected through the collection skill).
   - Optional context: `--model-id`, `--model-config`, `--hardware-model`, and `--hardware-profile` are passed to the remote analysis. Local `--model-config` / `--hardware-profile` files are uploaded into the remote run dir first.
3. **Dependency preflight**: select a supported remote Python that can import `yaml`. If no interpreter satisfies the skill's `requirements.txt`, return `phase=dependency_preflight` before syncing or running analysis.
4. **Parity sync** (light): tar-over-ssh only `scripts/ascend_profile/` from the local skill dir to `<remote-work-dir>/ascend_profile/`. Excludes `__pycache__` and `*.pyc`. Does **not** touch `.vaws-runtime/` or sync the entire repo.
5. **Remote analyze**: run `python3 -m ascend_profile.analyze <ROOT> --output <OUT> --verbose` from inside `<remote-work-dir>`. stdout/stderr is streamed back so the agent can see stage timings live.
6. **Validate artifacts**: every required artifact must exist, and `segment_manifest.json` must have `hard_errors == 0` and `interior_island_total == 0`.
7. **Pull artifacts**: mode-dependent. `fast` (default): `report/report.md` + `report/analysis_summary.json` + all `*_manifest.json` + class-level summary CSVs + `diagnosis_findings.json` (17 items). `full`: the previous lightweight set (`report/` incl. `assets/`, manifests, findings, summary CSVs, `step_segments.json`, `layer_segments.json`, `structure_evidence_graph.json`, `evidence_index.csv`). Use `--keep-remote-output` to mirror the entire remote output dir locally.
8. **Emit JSON** on stdout. Progress lines (`__VAWS_PROFILE_ANALYSIS_PROGRESS__=...`) go to stderr.

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
operator_efficiency_summary.csv
model_insights.json
model_context_summary.csv
model_inferred_config.csv
model_feature_summary.csv
model_layer_type_summary.csv
model_candidate_summary.csv
model_config_overview.csv
model_parameter_estimate.csv
model_kv_cache_estimate.csv
model_config_feature_summary.csv
hardware_insights.json
hardware_summary.csv
hardware_theoretical_peaks.csv
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

Excluded from the lightweight set (large or only useful for deep debug; pull on demand with `--keep-remote-output` or by explicit `remote_artifact_pull.py` against the remote output dir):

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
| `--model-id` / `--model-config` | Optional user/model registry context. Config is an optional comparison source; profiling-derived inference still works without it. For known models, missing layer count is resolved by enumerating catalog variants and fetching `config.json` from Hugging Face / ModelScope when needed; unresolved counts stay unknown rather than guessed. |
| `--hardware-model` / `--hardware-profile` | Optional capture-hardware context. Use it for historical roots; current remote host hardware is not proof of capture hardware. |
| CANN `platform_config` scan | Default source for theoretical hardware peaks. Disable with `--no-cann-hardware-scan` only for debugging. |

## Profiling-first model analysis

The skill infers a model fingerprint from profiling artifacts before looking
at any optional config file. The summary/report stage exports:

- `model_inferred_config.csv`: observed/candidate config fields, including
  layer count, hidden/intermediate/expert/head dimensions, profile sequence
  length, `vocab_size_or_lm_head_shard` when lm_head/logits shapes are visible,
  and a rank-visible matmul parameter lower bound.
- `model_feature_summary.csv`: observed architecture features from operator
  categories, for example MoE, MLA, CSA/HCA/DSA, dense flash attention, linear
  attention/Mamba/GDN, and RoPE.
- `model_layer_type_summary.csv`: layer/block structure sequences derived from
  block decomposition.
- `model_candidate_summary.csv`: local fingerprint-catalog candidate matches.
- `model_insights.json`: machine-readable rollup and limitations.

The candidate model table is a search aid, not a diagnosis conclusion. It may
match a model by exact vocab size or by a visible vocab shard that evenly
divides the catalog vocab. Single-rank captures report rank-visible weight
information only; full parameter totals require the TP/EP/DP strategy and
weight-sharding rules.

When a user supplies a fuzzy model family such as `dsv4` or `qwen3.5`, the
early model resolver enumerates the concrete variants registered in
`knowledge/model_fingerprints.json`.  Segment-stage model guidance may consume
only a concrete candidate whose public/local config or validated
profile-visible layer hint matches the current profile.  Quantization and data
format suffixes are ignored for structure selection and remain dtype/weight
metadata only.  Substring (non-exact) catalog hits are weaker evidence and are
capped at `medium` confidence; only an exact catalog-name match can report
`high`.  External `config.json` fetches are skipped for candidates that are
not plausible `org/name` repo ids, so family display names never turn into
real Hugging Face / ModelScope requests.

The early resolver has two fast knowledge paths before any statistical
fallback:

- Explicit user context: exact model ids, fuzzy family names, and structure
  descriptions are resolved through the catalog first.  Structure words such as
  CSA, DSA, HCA, compressor, lightning indexer, sparse shared-KV, and
  linear/Mamba map to catalog features.  `moe` / gating alone is generic
  architecture evidence only.
- Profiling operator fingerprint: when the user does not provide model context,
  the resolver scores the observed `op_categories` against each
  `model_fingerprints.json:operator_match` rule.  Examples: KV compressor is a
  DSV4 fast signal; lightning indexer plus sparse shared-KV and no compressor
  is DSA; MoE gating plus linear/Mamba is Qwen3.5/GDN-style hybrid evidence.

Plain feature-overlap matching remains available only as the last conservative
fallback and must not promote a single generic feature such as MoE into a
specific model or layer count.

## Hardware peak and MFU context

The skill separates three hardware concepts:

- capture hardware provenance: user input, collection manifest, hardware profile,
  or profiler metadata when available. Current analysis-host hardware is not
  assumed to be provenance for historical profiling roots.
- theoretical peak: derived from CANN `platform_config/*.ini` rows, for example
  `cube_core_cnt * cube_freq * M * N * K * 2`.
- sustained peak: practical operator-path factors from
  `knowledge/hardware_peak_measurements.json`.

Outputs:

- `hardware_theoretical_peaks.csv`: all CANN platform config rows discovered on
  the analysis host, with FP16/BF16/INT8 theoretical cube peaks.
- `hardware_summary.csv`: the selected hardware denominator and sustained
  factors.
- `hardware_insights.json`: machine-readable rollup and limitations.

Current measured knowledge includes Ascend910B4 / A2 32G from remote 131:

- FP16/BF16 dense matmul sustained factor: `0.95`.
- INT8 `npu_quant_matmul` sustained factor: `0.65`.

MFU denominators use theoretical peak. Operator roofline/reclaim ranking uses
sustained peak when a measured factor is available.  Only FP16/BF16/INT8 peaks
are modeled: INT4 and FP8/HiF8 dtypes use the INT8 op-rate peak as the closest
available denominator, and dtypes with no modeled peak report
`no_peak_for_dtype` instead of silently assuming FP16.

## Failure policy

Hard fail (`status: "failed"` in stdout JSON, non-zero exit code):

| Phase | Cause | exit code |
|-------|-------|-----------|
| `manifest_validation` | manifest missing / malformed / `analysis_status != "ok"` / `remote_profile_root` empty | 2 |
| `parity_sync` | tar-sync of `scripts/ascend_profile/` to remote failed | 3 |
| `remote_analyze` | remote `analyze.py` exited non-zero or hit `--remote-timeout` | 4 |
| `artifact_validation` | any required artifact missing, or `segment_manifest.json` reports `hard_errors > 0` / `interior_island_total > 0` | 5 |
| `artifact_pull` | artifact manifest / SSH-streaming pull back to the local run dir failed | 6 |

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
  → original kernel_details.csv (or the equivalent db-direct event stream from
    ascend_pytorch_profiler_*.db) / trace_view.json / op_summary / communication.json
```

If a claim cannot be backed at row level, the agent must surface it as a `limitation`, not a conclusion.

## What this skill does NOT do

- Start or stop services. Use `vllm-ascend-serving`.
- Run benchmarks. Use `vllm-ascend-benchmark`.
- Collect new torch profiler data (drive `/start_profile` / `/stop_profile`, run `analyse()`). Use `ascend-profiling-collection`.
- Attribute HBM / 显存. Use `ascend-memory-profiling`.
- Edit submodule code or push commits.
- Rewrite single-rank step boundaries from cross-rank evidence (the analysis framework intentionally forbids this).
