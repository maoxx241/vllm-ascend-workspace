# ascend-profiling-analysis tests

Lightweight unit/integration tests for the skill. Designed to run locally
without any Ascend NPU hardware, GPU, or remote SSH:

- `test_manifest_schema.py` — verifies that `segment_manifest.json`
  emits the `hard_error_count` / `interior_island_total` scalars
  required by the skill launcher.
- `test_html_diagnosis_key.py` — regression for the HTML report reading
  the `diagnosis_findings` key (not `findings`).
- `test_skill_contract.py` — verifies wrapper CLI accepts the documented
  arguments (`--manifest`, `--remote-profile-root`, `--local-output-dir`,
  `--skip-html`, `--report-mode`, stage selectors).
- `test_timeout.py` — verifies `ssh_stream` honours the wall-clock
  timeout even when the remote command produces no output.

Run from the repo root:

```bash
python3 -m pytest .agents/skills/ascend-profiling-analysis/tests/ -q
```

Or run an individual file directly:

```bash
python3 .agents/skills/ascend-profiling-analysis/tests/test_manifest_schema.py
```
