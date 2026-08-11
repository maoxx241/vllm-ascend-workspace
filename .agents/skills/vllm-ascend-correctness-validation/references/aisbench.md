# AISBench adapter

## Purpose

Use AISBench as a task-metric backend inside correctness validation. Do not make AISBench the source of run identity, environment truth, or pass/fail policy.

The adapter:

- generates an isolated custom `VLLMCustomAPIChat` model config;
- uses AISBench `--config-dir` without modifying the `benchmark` submodule;
- fixes `temperature=0` and an explicit seed;
- generates a reusable case file with metric direction and regression thresholds;
- converts `summary_*.csv` rows into the normalized correctness result contract.

## Prepare

```bash
python -B .agents/skills/vllm-ascend-correctness-validation/scripts/aisbench_adapter.py prepare \
  --output-dir .vaws-local/correctness/aisbench-baseline \
  --host 127.0.0.1 \
  --port 8000 \
  --served-model example \
  --dataset gsm8k_gen_4_shot_cot_str \
  --work-dir /remote/output/aisbench-baseline \
  --metric accuracy \
  --direction higher \
  --max-absolute-regression 0.5 \
  --max-relative-regression 0.01
```

The output contains:

```text
output-dir/
├── configs/models/vaws_correctness.py
├── aisbench-cases.json
├── command.json
└── run.sh
```

Run `run.sh` only where AISBench is installed and the target service is reachable.

## Normalize

Select the exact `summary_*.csv` produced by the run:

```bash
python -B .agents/skills/vllm-ascend-correctness-validation/scripts/aisbench_adapter.py normalize \
  --summary-csv /remote/output/summary/summary_20260725_120000.csv \
  --label baseline \
  --output /remote/output/baseline-normalized.json
```

Repeat with the candidate summary. Compare both normalized files with the same `aisbench-cases.json`.

## Metric scale

AISBench summaries may report percentages such as `56.70` rather than fractions such as `0.567`. Set absolute thresholds in the same scale as the selected summary. Do not silently rescale metrics.

## Secrets

The generated model config leaves `api_key` empty. If a service requires authentication, arrange it outside tracked configs and extend the remote runtime deliberately; never write a live key into the adapter inputs or artifacts.
