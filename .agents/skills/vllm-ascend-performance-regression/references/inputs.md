# Controlled experiment input

Use actual absolute business worktree paths for the local platform. The sample
thresholds below illustrate the format; choose values for the intended contract.
Code revisions, environments, devices and benchmark observations are collected
from the managed execution. A/B scheduling and warmup are internal.

```json
{
  "baseline": {
    "sources": {
      "vllm": "/src/baseline/vllm",
      "vllm-ascend": "/src/baseline/vllm-ascend"
    }
  },
  "candidate": {
    "sources": {
      "vllm": "/src/candidate/vllm",
      "vllm-ascend": "/src/candidate/vllm-ascend"
    }
  },
  "benchmark": {
    "model": "/models/example",
    "tp": 1,
    "bench_args": [
      "--dataset-name",
      "random",
      "--random-input-len",
      "512",
      "--random-output-len",
      "128",
      "--num-prompts",
      "64",
      "--max-concurrency",
      "8"
    ]
  },
  "runs": 3,
  "warmups": 1,
  "thresholds": {
    "output_throughput": {
      "direction": "higher",
      "max_relative_regression": 0.05
    },
    "mean_ttft_ms": {
      "direction": "lower",
      "max_relative_regression": 0.05
    }
  }
}
```

For existing measurements, pass `--results result-1.json result-2.json ...` with
the same run counts and thresholds. Missing or incomparable observations remain
inconclusive. An optional `fixed_dataset` object accepts `input_len`, `output_len`
and `num_rows`; the collector generates the data and records its content hash.
