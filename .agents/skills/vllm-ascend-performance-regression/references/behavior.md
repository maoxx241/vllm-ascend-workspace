# Performance regression behavior contract

## Experiment config

```json
{
  "schema_version": 1,
  "run_id": "performance-change-001",
  "baseline": {
    "label": "baseline",
    "code_snapshot": "abc123",
    "session_id": "perf-base"
  },
  "candidate": {
    "label": "candidate",
    "code_snapshot": "def456",
    "session_id": "perf-candidate"
  },
  "shared": {
    "machine": "npu-host",
    "npu_devices": [0, 1],
    "model": {
      "path": "/models/example",
      "weight_manifest_hash": "..."
    },
    "environment": {
      "cann": "...",
      "torch_npu": "..."
    },
    "topology": {
      "tp": 2
    },
    "serve_args": [],
    "bench_args": [],
    "dataset": "sharegpt"
  },
  "warmups": 1,
  "runs": 3,
  "max_cv": 0.1,
  "exclude_outliers": false,
  "thresholds": {
    "throughput": {
      "direction": "higher",
      "max_relative_regression": 0.03
    },
    "ttft": {
      "direction": "lower",
      "max_relative_regression": 0.05
    }
  }
}
```

All conditions except code snapshot, session ID, and display label belong in `shared`. Its canonical SHA256 is the required `config_hash`.

## Benchmark normalization

`normalize` reads either the Benchmark skill's single-run `metrics` object or
the `mean` values from its multi-run `aggregated` object. The default mapping is:

| Measurement | Benchmark field |
|---|---|
| `throughput` | `output_throughput` |
| `ttft` | `mean_ttft_ms` |
| `tpot` | `mean_tpot_ms` |
| `itl` | `mean_itl_ms` |
| `acceptance_rate` | `acceptance_rate` |

Add or replace mappings with `--metric-map TARGET=SOURCE`. Missing source fields
are omitted, so the analysis will mark a configured-but-missing metric
inconclusive.

## Measurement contract

Normalize one Benchmark result at a time:

```json
{
  "schema_version": 1,
  "state": "baseline",
  "phase": "measure",
  "ordinal": 1,
  "config_hash": "<64 lowercase hex>",
  "metrics": {
    "throughput": 1234.5,
    "ttft": 18.2,
    "tpot": 4.3,
    "itl": 4.2,
    "acceptance_rate": 0.91,
    "service_start_time": 42.0,
    "hbm": 61234
  },
  "source": "/path/to/benchmark-result.json"
}
```

The controller rejects out-of-order state, phase, ordinal, or config hash.

## Statistics

- exclude warmups;
- preserve all raw values;
- detect outliers with modified z-score based on median absolute deviation;
- exclude detected outliers from the decision only when `exclude_outliers=true`;
- compute arithmetic mean, sample standard deviation, and absolute coefficient of variation;
- compute relative change as `(candidate_mean - baseline_mean) / abs(baseline_mean)`;
- apply direction-specific degradation thresholds.

## Status

- `passed`: every required metric has at least two decision values per state, CV is within limit, and no threshold is exceeded;
- `failed`: measurement quality passes and at least one metric regresses;
- `inconclusive`: schedule incomplete, metrics missing or insufficient, or CV exceeds the configured limit.

Heavy profiler collection is a separate explicit action.
