# AISBench and prefix-tool installation

Use source installations for reproducibility and record both commit IDs. Run these commands on the benchmark client, which may be the server host rather than the inference container.

## AISBench Benchmark

Official repository: `https://github.com/AISBench/benchmark` (default branch `master`).

```bash
git clone https://github.com/AISBench/benchmark.git <benchmark-dir>
conda create --name ais_bench python=3.10 -y
conda activate ais_bench
cd <benchmark-dir>
python -m pip install -e ./ --use-pep517
python -m pip install -r requirements/api.txt
python -m pip install -r requirements/extra.txt
ais_bench -h
```

`requirements/api.txt` is required for service API benchmarks. Install only additional dataset or multimodal requirements needed by the case. Prefer a commit pin rather than silently updating an established environment.

Alternative PyPI installation is `python -m pip install ais_bench_benchmark`; source installation is preferred because the prefix tool creates and links custom model/dataset configs inside the source tree.

Record:

```bash
git -C <benchmark-dir> rev-parse HEAD
git -C <benchmark-dir> status --short
conda run -n ais_bench python -m pip freeze
conda run -n ais_bench ais_bench -h
```

## aisbench_auto_tools_prefix

Repository: `https://github.com/rayn-zzz/aisbench_auto_tools_prefix` (default branch `main`).

```bash
git clone https://github.com/rayn-zzz/aisbench_auto_tools_prefix.git <prefix-tool-dir>
git -C <prefix-tool-dir> rev-parse HEAD
conda run -n ais_bench python <prefix-tool-dir>/aisbench_test.py --help
```

Edit `<prefix-tool-dir>/config.py` and save a copy in every run. Required fields usually include:

```python
DATASET_PATH = "<writable-dataset-dir>"
WORK_PATH = "<absolute-benchmark-source-dir>"
MODEL_NAME = "<served-model-name>"
MODEL_PATH = "<tokenizer-or-model-path>"
HOST_IP = "<service-ip>"
HOST_PORT = "<service-port>"
DEFAULT_PERFORMANCE_TEST = "default_perf"
OUTPUT_DIR = "<session-or-run-specific-aisbench-output-dir>"
POD_INFO = ["<ip:port-per-observable-pod-or-dp-endpoint>"]
```

Create a unique `OUTPUT_DIR` for each run. Never reuse an old output directory when comparing configurations.

## Prefill command templates

0% prefix hit:

```bash
conda run -n ais_bench --no-capture-output python <prefix-tool-dir>/aisbench_test.py \
  --input_len <tokens> --output_len 1 \
  --data_num <concurrency-times-four> --concurrency <concurrency> \
  --request_rate 0 --test_type stream \
  --dataset_type prefix_cache --repeat_rate 0 \
  --dp <real-dp> --npu_num <total-npus> --seed <run-seed>
```

Nonzero prefix hit:

```bash
conda run -n ais_bench --no-capture-output python <prefix-tool-dir>/aisbench_test.py \
  --input_len <tokens> --output_len 1 \
  --data_num <concurrency-times-four> --concurrency <concurrency> \
  --request_rate 0 --test_type stream \
  --dataset_type prefix_cache --repeat_rate <ratio> \
  --prefix_test --dp <real-dp> --prefix_num <groups> \
  --npu_num <total-npus> --seed <run-seed>
```

`--prefix_test` warms `dp * prefix_num` prefixes so each DP cache domain is covered. Verify actual hit ratio from metrics rather than trusting only the requested `repeat_rate`.

## Compatibility checks

- If `--num-warmups` is unrecognized, align prefix-tool and AISBench versions; as a temporary compatibility patch, remove that option from the tool-generated command and record the change.
- If tokenizer loading fails, verify `transformers` compatibility with the model rather than changing the dataset length.
- If dataset generation reports exhausted picked IDs, preserve the error artifact, then remove or rotate the tool’s `picked_ids.txt` only after resolving its exact path.
- Disable benchmark-client proxy variables only if they prevent access to the local service; record this in the run configuration.
