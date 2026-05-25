# Profiling Analysis Command Recipes

All recipes assume the workspace root is the current working directory and the
target machine is already managed and ready (see `machine-management`).

## Single-root: from a collection manifest

The most common case. `ascend-profiling-collection` writes a manifest at
`.vaws-local/ascend-profiling-collection/runs/<timestamp>_<tag>/manifest.json`.
Feed that manifest to the analysis skill.

```bash
python3 .agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py \
  --manifest .vaws-local/ascend-profiling-collection/runs/20260507_qwen35_tp4_s3/manifest.json \
  --tag qwen35_tp4_s3
```

The skill reads `analysis_status`, `remote_profile_root`, and any
`session_file` / `session_id` recorded by collection. Session-scoped manifests
are analyzed in the same session container by default. It then pulls `report/`
plus the lightweight summaries back to
`.vaws-local/profiling-analysis/runs/<timestamp>_qwen35_tp4_s3/`.

To override the target explicitly:

```bash
python3 .agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py \
  --session-id scaffold-131-smoke \
  --manifest .vaws-local/ascend-profiling-collection/runs/20260507_qwen35_tp4_s3/manifest.json \
  --tag qwen35_tp4_s3
```

## Single-root: historical raw root

When the profiling root predates the collection skill (or was produced by an
external pipeline), pass `--remote-profile-root` directly:

```bash
python3 .agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py \
  --machine 173.131.1.2 \
  --remote-profile-root /tmp/prof_35b_tp4/s3 \
  --tag prof_35b_tp4_s3 \
  --verbose
```

The skill does not validate that the root looks like a torch profiler output;
it relies on the analysis pipeline to fail loudly if `kernel_details.csv` is
missing.

## Single-root: pull every artifact (deep debug)

When you need `normalized_event_index.csv` or `evidence/bubble_windows.jsonl`
locally (e.g. to grep for specific kernels), use `--keep-remote-output`:

```bash
python3 .agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py \
  --machine 173.131.1.2 \
  --remote-profile-root /tmp/prof_35b_tp4/s3 \
  --tag prof_35b_tp4_s3_full \
  --keep-remote-output
```

This can pull several GB per root. Prefer the default lightweight pull and
SSH into the remote for ad-hoc grep when possible.

## Multi-root sweep (regression baseline)

To re-run the published 61-root regression baseline on a single machine:

```bash
python3 .agents/skills/ascend-profiling-analysis/scripts/profile_sweep.py \
  --machine 173.131.1.2 \
  --search-root /vllm-workspace/.vaws-runtime/serving \
  --search-root /tmp \
  --search-root /home/m00663269/transfer_dsv4 \
  --tag full_regression \
  --verbose
```

The skill writes:

- `.vaws-local/profiling-analysis/runs/<timestamp>_full_regression/sweep_summary.json`
- per-root `report/` and `*_manifest.json` under that same dir

stdout is a single JSON with `root_count`, `status_counts`, `failed_roots`,
and a `layer_inventory` suitable for cross-capture comparison.

## Multi-root sweep: limited

For a quick smoke test (analyze the first 5 discovered roots only):

```bash
python3 .agents/skills/ascend-profiling-analysis/scripts/profile_sweep.py \
  --machine 173.131.1.2 \
  --search-root /tmp \
  --tag smoke \
  --limit 5
```

## Reading the artifacts

After analysis, the agent should consult the local run dir in this order:

1. `report/report.md` — narrative claims with `evidence_id` references.
2. `diagnosis_findings.json` — structured claims, confidence, limitations.
3. `segment_manifest.json` — sanity check `hard_errors`, `interior_island_total` (must be 0; the skill already enforces this, but reading the structure helps when investigating soft anomalies).
4. `cross_rank_alignment.csv` / `cross_rank_alignment.json` — for slow-rank, EP imbalance, or workload asymmetry investigations.
5. `step_summary.csv` / `layer_summary.csv` / `operator_summary.csv` — for raw timing / count breakdowns.
6. `evidence_index.csv` / `raw_kernel_index.csv` — to resolve an `evidence_id` back to source rows.

For Excel users, `report/report.xlsx` contains the same tables in a single
sortable workbook.

## Re-using a previous local run dir

This skill always creates a fresh local run dir. To compare two runs
(e.g. before vs after a code change), keep both `.vaws-local/profiling-analysis/runs/<ts>/`
directories and diff `step_summary.csv` / `diagnosis_findings.json`. There is
no built-in `--resume-run` because re-running analysis on the same remote
profile root is cheap (segment + summarize stages are deterministic).
