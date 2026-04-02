# Performance Probe Runbook

## Scope
- `vaws benchmark run` using the `vllm bench serve` wrapper.

## Inputs
- Benchmark preset name
- Either `--service-id` or `--weights-path` for an ephemeral service

## Run
- `vaws benchmark run --server-name <name> --preset qwen3_5_35b_tp4_perf --weights-path <remote-model-path>`

## Output
- Per-run summary keyed by `run_id`
- Saved result JSON from `vllm bench serve`

## Known Pitfalls
- `benchmark-temporary` sessions must be cleaned up automatically.
- Explicit services are reused only on exact fingerprint match.
