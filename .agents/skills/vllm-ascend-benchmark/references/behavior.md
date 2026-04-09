# Benchmark Skill Behavior

## Lifecycle

1. **Resolve machine** from local inventory (same as serving).
2. **Assemble config** from user args + optional nightly reference.
3. **Stop existing service** on the target machine if any.
4. **Start service** via `serve_start.py` (which handles parity sync internally).
5. **Run `vllm bench serve`** via SSH on the remote container.
6. **Collect results** from the bench output JSON.
7. **Stop service** via `serve_stop.py`.
8. **Output structured JSON** on stdout.

## Configuration Priority

User-provided arguments always take priority:

```
user CLI args  >  agent-assembled context  >  nightly YAML fallback
```

Nightly YAML is a reference source for discovering how to configure a model/feature, not an execution template. When `--refer-nightly` is given:

- `server_cmd` and `server_cmd_extra` are merged (minus `--tensor-parallel-size` and `--port`, which are handled separately).
- `envs` are used as a base, with user `--extra-env` overriding.
- `benchmarks.perf` fields (`num_prompts`, `max_out_len`, `batch_size`) are mapped to bench CLI args.
- User-provided `--serve-args` / `--bench-args` completely override the nightly values.

## A/B Comparison

The A/B flow runs two complete benchmark cycles (start -> bench -> stop) sequentially:

1. Checkout `baseline-ref` in the target submodule, run full bench cycle.
2. Checkout `patched-ref`, run full bench cycle.
3. Compute delta and regression status.
4. Restore the submodule to its original HEAD.

### Patched Extra Env

The patched side can carry additional environment variables via `--patched-extra-env`. These are:

- Applied only to the patched service launch.
- Recorded in the output under `env_diff.patched_only`.
- Typical use case: the patched branch introduces a new feature flag (e.g., `VLLM_ENABLE_NEW_OPT=1`) that must be set for the optimization to take effect.

### Core Parameters Invariant

Both runs share identical:
- Model path and served model name
- TP / port configuration
- Bench args (num-prompts, concurrency, output-len, etc.)
- Base environment variables (from `--extra-env`)

Only the code state and `--patched-extra-env` differ.

## Remote Execution

`vllm bench serve` runs inside the container via SSH. The result JSON file is written to `/tmp/` and `cat`-ed back through the SSH session. The script parses the last JSON object from stdout.

## Defaults

When the user provides no bench args and no nightly reference:
- `--num-prompts 64`
- `--max-concurrency 16`

These are conservative defaults suitable for a quick smoke test. For production benchmarking, users should specify explicit parameters.

## State Management

Benchmark results are not persisted locally by default. The structured JSON is returned on stdout for the agent or user to consume. The serving skill handles its own state under `.vaws-local/serving/`.
