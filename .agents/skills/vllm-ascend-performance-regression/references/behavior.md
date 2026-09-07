# Performance regression behavior contract

## Experiment config

```json
{
  "schema_version": 1,
  "run_id": "performance-change-001",
  "parent_run_id": "change-validation-001",
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
      "tp": 2,
      "dp": 1
    },
    "serve_args": [],
    "bench_args": [],
    "dataset": "sharegpt",
    "max_concurrency": 16,
    "request_rate": "inf"
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

All conditions except code snapshot, session ID, and display label belong in `shared`. Its canonical SHA256 is the required `config_hash`. `parent_run_id` is optional and names the change-validation plan this experiment is evidence for; `change_validation.py link` refuses manifests without it.

### Required `shared` keys

`plan` rejects a config unless `shared` records every condition the parity certificate claims to hold constant:

| Key | Constraint |
|---|---|
| `machine` | non-empty string |
| `npu_devices` | non-empty array of non-negative device indices |
| `model` | non-empty object (path and weight hash) |
| `environment` | non-empty object (CANN, torch_npu, driver versions) |
| `topology` | object with positive-integer `tp` and `dp`; record `1` explicitly |
| `serve_args` | array of strings |
| `bench_args` | array of strings |
| `dataset` | non-empty string |
| `max_concurrency` | positive integer |
| `request_rate` | positive number or `"inf"` |

The rejection lists every missing or malformed key. Hashing a free-form object such as `{"note": "same"}` would produce a `config_hash` that certifies nothing, so it is not allowed.

## Parity check

`parity-check.json` states what it verified and on what basis:

- `basis`: `declared-configuration`. The certificate proves the operator recorded every required non-code condition once and that both states are pinned to the same declaration by `config_hash`;
- `checks`: required keys present, `topology.tp`/`topology.dp` recorded, sessions distinct, code snapshots recorded;
- `not_checked`: the observed runtime configuration of either service, raw Benchmark artifact contents, and whether a measurement labelled `baseline` really came from that state.

Read `not_checked` before citing the certificate; it is a declaration gate, not an observation of what ran.

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

Optional `observation` is a non-empty object recorded from the run that actually
produced the measurement (code digest, environment, model, topology, serve/bench
args, concurrency, request rate, devices, native digest). `analyze` will not
emit `passed` without it.

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

- `passed`: a comparable observational certificate was consumed, every required metric has at least two decision values per state, CV is within limit, and no threshold is exceeded;
- `failed`: the pair is comparable, measurement quality passes, and at least one metric regresses;
- `inconclusive`: schedule incomplete, metrics missing or insufficient, or CV exceeds the configured limit.

`analyze` issues the certificate from each state's recorded measurement `observation` plus the declared `shared` config. `shared` stays declared. Missing observations, declared-only must-observe keys, or an undeclared difference abort `analyze` before it can emit `passed`. Default variable under test is `workspace_snapshot.vllm_ascend_commit`; override with config `allowed_differences`. See `docs/comparability-certificate.md`.

Heavy profiler collection is a separate explicit action.
