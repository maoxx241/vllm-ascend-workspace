---
name: optimize-vllm-ascend-prefill
description: Benchmark and optimize vLLM Ascend Prefill performance from a user workload case, service launch script, and TTFT SLO. Use when Codex must install or use AISBench and aisbench_auto_tools_prefix, search concurrency and Prefill scheduler parameters, validate prefix-cache behavior, select the highest-throughput valid configuration under a TTFT limit, and archive AISBench outputs, console logs, vLLM Ascend service logs, /root/ascend/log, hardware/container/image metadata, git commits, and final results. Designed for Prefill output-length-1 testing; Decode optimization is a planned extension.
---

# Optimize vLLM Ascend Prefill

Find the highest valid Prefill throughput under the requested TTFT SLO, preserve every experiment, and leave a reproducible best service script.

For workspace-managed remote targets, use the `.remote-dev` companion tools for remote reads, writes, commands, monitoring, and artifact transfer. Do not replace them with raw SSH. Run the bundled scripts on the benchmark endpoint when they need direct access to Docker, AISBench files, or `/root/ascend/log`.

Bundled helpers keep diagnostic progress on `stderr` and emit one final machine-readable JSON object on `stdout`. Treat `archive_run.py` status `incomplete` as missing evidence, not a fully archived run.

## Required inputs

Resolve these before mutating the server. Discover missing values read-only when possible.

- Server access and target container.
- Model case: input length, output length (force `1` for Prefill), prefix-cache hit ratio, prefix count, and DP size.
- TTFT limit and metric (`average`, `P90`, or another percentile). Never guess the metric; default to P90 only when the user says merely “TTFT limit” and state the assumption.
- Service launch script, health endpoint, API port, and reset-prefix-cache endpoint.
- Allowed tuning parameters and safe ranges. Typical knobs are `--max-num-batched-tokens`, `--long-prefill-token-threshold`, `--max-num-seqs`, parallel topology, and compilation features.
- Starting concurrency. Use `data_num = concurrency * 4` unless the user specifies otherwise.
- Result root. Create one timestamped session folder; never mix raw artifacts from separate sessions. When the user does not provide a path, use untracked `.vaws-local/prefill-optimization/`.

Read [references/installation.md](references/installation.md) when AISBench or the prefix tool is missing. Read [references/search-and-validation.md](references/search-and-validation.md) before designing the experiment matrix. Read [references/result-layout.md](references/result-layout.md) before the first run.

## Safety invariants

1. Preserve the user’s launch script. Create a versioned copy for every service configuration.
2. Resolve the exact container with `docker inspect` before clearing logs.
3. Immediately before each service start, run `scripts/clear_ascend_logs.sh --container <name>`. This script only clears the fixed container path `/root/ascend/log`.
4. Archive the current run before clearing logs for the next service start.
5. Do not declare an experiment successful from the process return code alone. Require valid output tokens and all requests successful.
6. Do not treat HTTP 500 responses, zero-token responses, OOM, engine death, or retry exhaustion as performance samples.
7. Keep the service running after the final test unless the user requests shutdown or another configuration must be started.
8. Never store passwords, API keys, access tokens, or credential-helper output in the result folder.
9. For repository workflows, create and maintain Run Manifest v1 with run type `performance`; keep the domain-specific `result.json` files as complementary experiment records.

## Workflow

### 1. Initialize and inventory

Create the session layout from [references/result-layout.md](references/result-layout.md). Copy the original launch script into `session/config/original_service_script.sh`.

Initialize `session/run_manifest.json` through `.agents/scripts/run_manifest.py` or `.agents/lib/vaws_run_manifest.py`. Transition it from `planned` to `running`, then to `passed`, `failed`, `inconclusive`, or `cancelled`, and register the final report, experiment table, best result, and best service script as artifacts. Validate the finished manifest before reporting completion.

Record before testing:

- Hostname, date/timezone, OS, kernel, CPU, memory, disks, NPU topology and `npu-smi info`.
- Container name/ID, full `docker inspect`, image ID/digest/tags and image inspect.
- vLLM and vLLM Ascend repository paths, branch, commit ID, `git status --short`, package versions, CANN/torch/torch-npu versions when available.
- Original service command and relevant environment variables.
- AISBench and aisbench_auto_tools_prefix commit IDs and Python environment package snapshot.

Do not infer versions from directory names when a command can report them.

### 2. Validate the workload

For Prefill:

- Set output length to `1`.
- Keep input length and prefix hit ratio equal to the user case.
- Use `--dataset_type prefix_cache` even for 0% when consistent dataset generation is required.
- For hit ratio greater than 0, use `--prefix_test`, set the real `--dp`, and set `--prefix_num` to the requested number of prefix groups.
- For 0% hit, reset prefix cache immediately before the measured run and omit `--prefix_test`.
- Use `--request_rate 0 --test_type stream` unless the case says otherwise.

Save the exact generated dataset path, tokenizer/model path, seed, command, and a copy of prefix-tool `config.py`.

### 3. Establish a valid baseline

Start from the supplied service script with the smallest required edit. Before launch:

```bash
scripts/clear_ascend_logs.sh --container "$CONTAINER"
```

On a remote target, invoke this endpoint-side command with `remote.bash` and monitor long-running service or benchmark commands with `remote.monitor`. Use the repository compatibility wrappers only when a managed session requires them.

Start the service with stdout/stderr redirected to the run’s `logs/service.log`. Poll health with a bounded timeout. On startup failure, archive logs and mark the run invalid; do not proceed to AISBench.

Reset prefix cache before the measured phase. Capture `/metrics` and `npu-smi info` before, during, and after the test. Run the exact AISBench command while teeing the full terminal output to `logs/aisbench_console.log`.

### 4. Search for the optimum

Use the staged search in [references/search-and-validation.md](references/search-and-validation.md):

1. Sweep concurrency with the baseline service configuration.
2. Tune one service parameter family at a time near the best feasible concurrency.
3. Re-sweep concurrency for promising configurations.
4. Confirm the winning point with a fresh service start, fresh Ascend logs, cache reset/warmup as required, and a full `concurrency * 4` dataset.

Primary optimization objective: highest QPS among runs satisfying the requested TTFT limit. For fixed input length and output=1, input-token throughput is a consistency check and secondary tie-breaker. If input lengths vary, report both QPS and input-token throughput and ask the user which objective governs before declaring a winner.

Do not stop at the first TTFT failure if a nearby scheduler configuration may restore feasibility. Bound the search and record why it stopped.

### 5. Validate every run

A run is valid only when all conditions hold:

- Service stayed healthy and no worker/engine fatal error occurred.
- AISBench submitted the intended request count and every request produced one output token.
- Failed request count is zero.
- Actual concurrency and actual average input length are recorded.
- Prefix-cache hit ratio derived from metric deltas matches the target within the declared tolerance.
- TTFT metric is finite and below the SLO (`<`, not `<=`, unless the user specifies otherwise).
- No OOM or NPU fatal error appears in service or Ascend logs.

Write `result.json` using the schema in [references/result-layout.md](references/result-layout.md). Use `scripts/parse_aisbench_console.py` to extract common AISBench metrics, then add service parameters, cache deltas, validity, and artifact paths.

### 6. Archive before the next service start

Run `scripts/archive_run.py` after each test and before any next log cleanup. It copies:

- AISBench output directory and timestamped `outputs` tree.
- `aisbench.log`, `aisbench_result.csv`, prefix-tool config and generated command.
- Full AISBench console log.
- Full vLLM Ascend service log.
- Container `/root/ascend/log`.
- Metrics, NPU samples, server/container/image/version metadata.
- The exact service script used by that run.

The script creates a SHA-256 manifest. If any required artifact is missing, mark that in the run notes rather than silently claiming complete archival.

### 7. Select and report

After all `result.json` files exist, run:

```bash
python scripts/summarize_runs.py \
  --session-dir <session> \
  --ttft-limit-s <limit> \
  --ttft-metric p90 \
  --objective qps
```

Review the generated `summary/experiments.csv`, `summary/best.json`, and `summary/report.md`. Copy the winning service script to `best/best_service_script.sh` without removing its run-local copy.

The final response must state:

- Best topology and service parameters.
- Concurrency/data count and actual DP distribution when observable.
- TTFT average and constrained percentile, QPS, input throughput, prefix hit ratio, and peak KV-cache usage.
- Count of successful, failed, and invalid experiments.
- Exact session folder and best-run artifact paths.
- Whether the final service is still running.

## Decode extension boundary

Do not reuse Prefill validity or objective rules for Decode. A future Decode mode should use very high prefix hit, preserve requested input/output lengths, optimize output-token throughput under a TPOT constraint, and add acceptance/speculative-decoding metrics. Keep `case_type: "prefill"` in current result files so Decode can be added without schema ambiguity.
