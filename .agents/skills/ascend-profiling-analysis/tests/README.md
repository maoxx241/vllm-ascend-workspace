# ascend-profiling-analysis tests

Lightweight unit/integration tests for the skill. Designed to run locally
without any Ascend NPU hardware, GPU, or remote SSH:

- `test_analysis_summary.py` — pins the agent-first output contract:
  `report/analysis_summary.json` schema (top-level keys, layer_validation
  expected/detected layer mapping, KPI derivations, null tolerance), the
  findings rollup (grouping key, occurrences, severity×occurrences ordering,
  the 50-group cap + `rollup_overflow`, `knowledge_refs` placeholder), the
  fast-mode skips (`render_report --skip-xlsx` drops the workbook and records
  `xlsx_status=skipped`; `summarize --skip-host-trace` marks
  `host_trace.status=skipped` with bubble `soft_attribution=null`), and the
  wrapper plumbing (`--mode fast|full`, `FAST_PULL_PATHS`,
  `REQUIRED_SINGLE_ARTIFACTS_FAST`, mode-dependent remote flags, local
  analysis_summary read-back).
- `test_attention_families.py` — pins the attention family resolver
  (`rules.resolve_attention_family`) against kernel bags from real traces,
  and verifies the HTML report (`html_report.detect_attention_subtype`) uses
  the same resolution so test contract and report output cannot drift.
- `test_bubble_attribution.py` — covers the idle-pattern detection salvaged
  from the retired user-level `ascend-profiling-anomaly` skill: edge-gap
  anomaly tags (`PRELAUNCH_GAP_HEAVY` / `TAIL_GAP_HEAVY`), the rank-level
  recurring-bubble rollup + `recurring_bubble_pattern` finding,
  `PARTIAL_CAPTURE_BOUNDARY` conservatism, and host-side soft attribution
  (`host_trace.py`) with and without `trace_view.json`.
- `test_hardware_insights.py` — verifies hardware peak loading
  (static theoretical peaks, CANN platform config parsing,
  `peak_flops_per_second`) and the derived operator-efficiency rows.
- `test_html_diagnosis_key.py` — regression for the HTML report reading
  the `diagnosis_findings` key (not `findings`).
- `test_knowledge_enrichment.py` — pins the wrapper-side workspace knowledge
  hooks (`profile_analyze._enrich_analysis_summary_with_knowledge`): findings
  rollup groups get `knowledge_refs` from a temporary synthetic knowledge dir
  (hit / miss / empty store / missing dir / invalid document), and
  `layer_validation.expected_layers` backfill fires only when the pipeline
  left it null (source marked `knowledge:<entry_id>`, `layers_match`
  recomputed, ok→degraded on mismatch, config-driven entries skipped).
- `test_kernel_signatures.py` — pins the contract between Python's
  `categories_and_roles` rule list and `knowledge/kernel_signatures.yaml`:
  the YAML parses, its categories are valid per
  `semantic_conventions.yaml:op_categories`, and curated kernel names from
  real traces resolve to the categories the YAML claims.
- `test_manifest_schema.py` — verifies that `segment_manifest.json`
  emits the `hard_error_count` / `interior_island_total` scalars
  required by the skill launcher.
- `test_model_context.py` — verifies model-context resolution
  (`resolve_model_context`) from fingerprints and profile evidence, with
  network access mocked out.
- `test_model_insights.py` — verifies model-insight derivation
  (`candidate_model_rows`, `model_config_insights`,
  `operator_efficiency_rows`, `profile_inferred_model_insights`).
- `test_moe_families.py` — family-resolution tests for MoE / FFN: kernel
  bags from real DSV / Qwen-MoE traces resolved against the cheat-sheet in
  `moe_families.yaml`.
- `test_segment_validator.py` — pins the segment-stage exact-cover
  validation contracts (multi-layer plans that are themselves recurring
  templates must not false-positive).
- `test_semantic_conventions.py` — pins the Python↔YAML enum contract for
  `knowledge/semantic_conventions.yaml`: every value Python emits is listed
  in the YAML, and the YAML lists no dead values.
- `test_skill_contract.py` — verifies wrapper CLI accepts the documented
  arguments (`--manifest`, `--remote-profile-root`, `--local-output-dir`,
  `--skip-html`, `--report-mode`, stage selectors).
- `test_stage_validation.py` — pins the wrapper's stage-aware artifact
  validation (`REQUIRED_ARTIFACTS_BY_END_STAGE`), so partial-stage runs are
  not rejected for missing full-pipeline artifacts.
- `test_timeout.py` — verifies `ssh_stream` honours the wall-clock
  timeout even when the remote command produces no output.
- `test_work_estimates.py` — regression tests for the shape-derived
  byte/FLOP estimates in `ascend_profile.work` (substring-matched factor
  tables).

`pytest` is the only test dependency beyond the runtime requirements in
`../requirements.txt` (some knowledge-contract tests additionally need
PyYAML and skip themselves when it is missing).

Run from the repo root:

```bash
python3 -m pytest .agents/skills/ascend-profiling-analysis/tests/ -q
```

Or run an individual file directly:

```bash
python3 .agents/skills/ascend-profiling-analysis/tests/test_manifest_schema.py
```
