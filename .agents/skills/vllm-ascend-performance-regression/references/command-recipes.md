# Performance regression command recipes

## Plan

```bash
python -B .agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py plan \
  --output-dir .vaws-local/performance-regression/change-001 \
  --config /path/to/experiment-config.json
```

Read the returned config hash and `schedule.json`.

## Normalize and record

After executing the next scheduled Benchmark:

```bash
python -B .agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py normalize \
  --result /path/to/raw-benchmark-result.json \
  --output /path/to/normalized-measurement.json \
  --state baseline \
  --phase measure \
  --ordinal 1 \
  --config-hash <hash-from-plan>

python -B .agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py record \
  --output-dir .vaws-local/performance-regression/change-001 \
  --result /path/to/normalized-measurement.json
```

Do not skip ahead. The script returns the remaining entry count.

## Analyze

```bash
python -B .agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py analyze \
  --output-dir .vaws-local/performance-regression/change-001
```

Inspect `report.md`, then use the linked raw Benchmark results for deeper review.
