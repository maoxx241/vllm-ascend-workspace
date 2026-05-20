# Profiling Analysis Skill Acceptance Criteria

## Single-root (`profile_analyze.py`)

### Input contract

- [ ] Exactly one of `--manifest` or `--remote-profile-root` is required.
- [ ] When `--manifest` is given, the script reads `analysis_status` and refuses to proceed unless it equals `"ok"`.
- [ ] When `--manifest` is given, the script copies the input manifest into the local run dir as `collection_manifest.json` for traceability.
- [ ] When neither input is supplied (or both are), argparse rejects the invocation.
- [ ] `--local-output-dir` is honoured: when set, pulled artifacts land there; existing non-empty targets are rejected unless `--overwrite` is also given.
- [ ] `--skip-html` / `--report-mode {summary,interactive,full-raw}` / `--from-stage` / `--to-stage` / `--only-stage` are forwarded verbatim to the remote `ascend_profile.analyze` invocation.

### Remote orchestration

- [ ] Only `scripts/ascend_profile/` is tar-synced to the remote (no full repo sync, no `.vaws-runtime` mutation).
- [ ] `__pycache__` and `*.pyc` are excluded from the tar.
- [ ] The remote command is `python3 -m ascend_profile.analyze <ROOT> --output <OUT> [--verbose]`, executed inside `<remote-work-dir>`.
- [ ] Remote stdout/stderr is streamed to local stderr with a `[ascend_profile]` prefix.
- [ ] `--remote-timeout` is enforced; on timeout the script returns `status: "failed"` with `phase: "remote_analyze"`.

### Artifact validation

- [ ] All of these must exist in the remote output dir:
  - `manifest.json`
  - `segment_manifest.json`
  - `diagnosis_findings.json`
  - `report/report.md`
  - `report/report.xlsx`
  - `report/report.html` (stub on render failure; `report/manifest.json:html_status` reports the actual state)
- [ ] If any required artifact is missing, the script returns `status: "failed"` with `phase: "artifact_validation"`.
- [ ] If `segment_manifest.json` reports `hard_error_count > 0` (or legacy list-form `hard_errors` non-empty) or `interior_island_total > 0`, the script returns `status: "failed"` with `phase: "artifact_validation"`.
- [ ] `render_report` raises `RuntimeError` when any diagnosis finding lacks both evidence ids and an explicit limitation; the wrapper surfaces that as `phase: "remote_analyze"` with the evidence-chain error message.

### Artifact pull

- [ ] By default, only the lightweight pull set is rsynced back (see `behavior.md`).
- [ ] `--keep-remote-output` rsyncs the entire remote output dir locally.
- [ ] `normalized_event_index.csv` and `evidence/bubble_windows.jsonl` are excluded from the lightweight pull.

### Output JSON

- [ ] On success, stdout JSON contains: `status: "ok"`, `machine`, `remote_profile_root`, `remote_output_dir`, `local_output_dir`, `stage_timings`, `rank_count`, `event_count`, `segment_count`, `layer_count`, `diagnosis_counts`, `report_md`, `report_xlsx`, `report_html`, `html_status`, `elapsed_s`.
- [ ] On failure, stdout JSON contains: `status: "failed"`, `phase`, `error`, plus context (`machine`, `remote_profile_root`, `remote_output_dir` where applicable).
- [ ] Progress lines on stderr are prefixed with `__VAWS_PROFILE_ANALYSIS_PROGRESS__=`.
- [ ] Final JSON is the only thing written to stdout.

### Local state

- [ ] All local state is written under `.vaws-local/profiling-analysis/runs/<timestamp>_<tag>/`.
- [ ] The local run dir contains a `skill_run.json` with machine, remote paths, stage timings, and elapsed time.
- [ ] No files are written outside the local run dir or the remote work dir.

## Multi-root (`profile_sweep.py`)

### Input contract

- [ ] At least one `--search-root` is required (repeatable).
- [ ] `--limit` caps the number of analyzed roots; the discovery order is the same as `ascend_profile.sweep`.
- [ ] `--jobs N` is forwarded to the remote sweep (defaults to 1, sequential).
- [ ] `--reuse-existing` is forwarded; sweep skips roots whose `manifest.json` already exists in the per-root output dir.
- [ ] `--render-html` flips the remote sweep into HTML-rendering mode; default behaviour is `--skip-html` so sweep outputs stay small. `--pull-html` is independent: it only controls whether the local pull set includes `report/report.html`.

### Remote orchestration

- [ ] `scripts/ascend_profile/` is tar-synced before the sweep starts.
- [ ] The remote command is `python3 -m ascend_profile.sweep --search-root ... --output <OUT> [--verbose]`.
- [ ] `--remote-timeout` defaults to 14400s (matches the 61-root regression baseline).

### Output contract

- [ ] `sweep_summary.json` is always pulled back to the local run dir.
- [ ] When the remote sweep partially fails (one or more roots errored), `status` is `"partial"` and the failing roots are listed in `failed_roots` on stdout.
- [ ] The skill returns exit code 1 when `status == "partial"`.
- [ ] The skill returns `status: "failed"` (exit code 3-6) only when the sweep failed before it could write `sweep_summary.json`, or when artifact pull fails.
- [ ] `layer_inventory` is computed from the union of `rank_layer_inventory` keys per root.

### Artifact pull

- [ ] By default, only `sweep_summary.json` and per-root `report/` + `*_manifest.json` + summary CSVs are pulled.
- [ ] `--keep-remote-output` mirrors the entire remote sweep dir locally.

## Cross-platform

- [ ] Scripts use `python3` everywhere; no shebang dependence on a specific interpreter path.
- [ ] All argparse parsers set `allow_abbrev=False` to prevent accidental prefix matching.

## Boundaries (regression-protective)

- [ ] The skill never starts or stops a vLLM service.
- [ ] The skill never invokes `/start_profile` / `/stop_profile`.
- [ ] The skill never modifies `.vaws-runtime/` on the remote.
- [ ] The skill never edits files inside `vllm/` or `vllm-ascend/` submodules.
- [ ] The skill wrapper does not duplicate any logic from `scripts/ascend_profile/*.py`; it only orchestrates remote execution.

## Automated tests

- [ ] `python3 .agents/skills/ascend-profiling-analysis/tests/test_skill_contract.py` exits 0 (wrapper CLI surface matches the doc).
- [ ] `python3 .agents/skills/ascend-profiling-analysis/tests/test_manifest_schema.py` exits 0 (segment manifest schema regression).
- [ ] `python3 .agents/skills/ascend-profiling-analysis/tests/test_html_diagnosis_key.py` exits 0 (HTML diagnosis key regression).
- [ ] `python3 .agents/skills/ascend-profiling-analysis/tests/test_timeout.py` exits 0 (silent-hang wall-clock regression).
