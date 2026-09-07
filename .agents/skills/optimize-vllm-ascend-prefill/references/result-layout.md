# Result layout and result.json schema

Use one timestamped session root and one immutable directory per experiment.

```text
<session>/
├── run_manifest.json
├── session.json
├── config/
│   ├── original_service_script.sh
│   └── workload.json
├── environment/
│   ├── server/
│   ├── container/
│   ├── image/
│   └── versions/
├── runs/
│   └── 000_<short-name>/
│       ├── result.json
│       ├── config/
│       │   ├── service_script.sh
│       │   ├── aisbench_command.txt
│       │   └── prefix_tool_config.py
│       ├── logs/
│       │   ├── service.log
│       │   ├── aisbench_console.log
│       │   └── ascend/
│       ├── aisbench/
│       │   ├── outputs/
│       │   ├── aisbench.log
│       │   └── aisbench_result.csv
│       ├── metrics/
│       │   ├── before.txt
│       │   ├── timeseries.log
│       │   └── after.txt
│       ├── npu/
│       └── manifest.sha256
├── summary/
│   ├── experiments.csv
│   ├── best.json
│   └── report.md
└── best/
    └── best_service_script.sh
```

`run_manifest.json` is the repository Run Manifest v1 contract for the overall performance workflow. `session.json` and per-run `result.json` retain Prefill-specific case, scheduler, cache, and latency details.

## session.json

```json
{
  "case_type": "prefill",
  "created_at": "ISO-8601",
  "ttft_limit_s": 15.0,
  "ttft_metric": "p90",
  "objective": "qps",
  "workload": {
    "input_len": 131072,
    "output_len": 1,
    "prefix_hit_target": 0.9,
    "prefix_num": 1,
    "dp": 2,
    "data_multiplier": 4
  }
}
```

## result.json

```json
{
  "run_id": "007_mbt40960_cc20",
  "case_type": "prefill",
  "status": "success",
  "parameters": {
    "concurrency": 20,
    "data_num": 80,
    "max_num_batched_tokens": 40960,
    "long_prefill_token_threshold": 2048,
    "max_num_seqs": 40,
    "tp": 4,
    "pp": 2,
    "dp": 2
  },
  "workload": {
    "requested_input_len": 131072,
    "actual_input_len_avg": 131248.0,
    "requested_output_len": 1,
    "output_tokens_avg": 1.0,
    "prefix_hit_target": 0.9,
    "prefix_hit_actual": 0.9
  },
  "requests": {
    "total": 80,
    "successful": 80,
    "failed": 0,
    "actual_concurrency": 19.9
  },
  "metrics": {
    "ttft_avg_s": 12.1,
    "ttft_p90_s": 14.2,
    "ttft_p99_s": 14.8,
    "qps": 1.1,
    "input_token_throughput": 144000.0,
    "peak_kv_cache_usage": 0.82
  },
  "validity": {
    "valid": true,
    "ttft_slo_pass": true,
    "output_pass": true,
    "request_pass": true,
    "prefix_hit_pass": true,
    "service_healthy": true,
    "reasons": []
  },
  "artifacts": {
    "service_script": "config/service_script.sh",
    "service_log": "logs/service.log",
    "aisbench_console": "logs/aisbench_console.log",
    "aisbench_outputs": "aisbench/outputs",
    "ascend_logs": "logs/ascend"
  }
}
```

Use seconds for normalized latency metrics and raw fractions (`0.9`, not `90`) for utilization/hit ratios. Preserve raw AISBench units in its original files.

## archive_run.py invocation

Run on the benchmark server after the measured command finishes:

```bash
python scripts/archive_run.py \
  --run-dir <session>/runs/007_mbt40960_cc20 \
  --container <container> \
  --service-log <live-service-log> \
  --console-log <tee-console-log> \
  --aisbench-output-dir <unique-output-dir> \
  --aisbench-log <prefix-tool-dir>/aisbench.log \
  --aisbench-result <prefix-tool-dir>/aisbench_result.csv \
  --service-script <candidate-service-script> \
  --prefix-config <prefix-tool-dir>/config.py \
  --aisbench-command <saved-aisbench-command.txt> \
  --aisbench-repo <benchmark-source-dir> \
  --prefix-tool-repo <prefix-tool-dir> \
  --conda-env ais_bench \
  --vllm-repo <container-vllm-path> \
  --vllm-ascend-repo <container-vllm-ascend-path>
```

Archive before clearing container Ascend logs for the next service configuration.
