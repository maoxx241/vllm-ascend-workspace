# Behavior Reference

## Why profiler control lives here, not in the serving skill

`/start_profile` and `/stop_profile` only exist because vLLM has a built-in torch profiler. Moving the control client into `vllm-ascend-serving` would force serving to grow profiling-specific knobs (multi-rank long timeout, multi-api-server quirks). The serving skill stays simple by treating `--profiler-config` as an opaque blob it forwards to `vllm serve`. Anything that flips, waits on, or interprets the profiler window belongs here.

## Mode → vLLM flag mapping

| `--mode` | Forwarded to `vllm serve` |
| --- | --- |
| `enforce_eager` | `--enforce-eager` |
| `full_decode_only` | `--compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}'` |
| `piecewise_graph` | `--compilation-config '{"cudagraph_mode":"PIECEWISE"}'` |

`CUDAGraphMode` enum values come from `vllm/vllm/config/compilation.py`. Add a new mode here only after verifying the underlying enum value still exists in the active `vllm/` submodule.

## Speculative config

`--speculative-tokens 0` (the default) means **omit** `--speculative-config` entirely. A positive value writes:

```json
{"method": "<--speculative-method>", "num_speculative_tokens": <N>}
```

Old prototypes always wrote a config block with `num_speculative_tokens=3`; that became confusing for non-MTP cases. The collection skill is now explicit.

## Control-plane timeout

`/start_profile` and `/stop_profile` setup/finalization touches every rank. With TP=8 it is normal to see 60+ seconds of latency on each call. The default `--profile-control-timeout 600` covers most cases; bump to 1200–1800 for large-scale runs. `--request-timeout` is independent and applies only to the chat-completions requests during the profile window.

## Request wave shape

The default wave is `--benchmark-total-requests 10` at `--benchmark-concurrency 5`, followed by exactly one `--followup-output-tokens 5` tail request. The follow-up exists so the resulting trace contains a clean steady-state step that is not contaminated by the wave's tail decay. The analysis skill relies on this shape for its tail-step detection.

## Workload transport

Chat requests are sent from the local machine through an ephemeral `ssh -L` tunnel. This is so multimodal (`--request-kind vl`) payloads — base64 image data URLs — can be assembled locally without round-tripping through SSH heredocs. For `--request-kind text` the tunnel is still used (consistency); request bodies stay small so there is no transport cost.

The `sips` image resizer is macOS-only. The collection skill assumes the agent runs on a Mac workstation. If the workstation is ever Linux, swap `_parse_sips_dimensions` / `_build_image_data_url` for a PIL implementation.

## Multi-api-server interactions

`--api-server-count` is exposed because vLLM's multi-api-server mode has historically had control-plane routing quirks: a `/start_profile` POST may be answered by an API server that is not the one driving the worker processes. When investigating a "trace empty" symptom, set `--api-server-count 1` to eliminate this variable before blaming the profiler.

## Output verification

After `analyse()`, every `*_ascend_pt` directory is expected to contain:

- `ASCEND_PROFILER_OUTPUT/kernel_details.csv`
- `ASCEND_PROFILER_OUTPUT/trace_view.json`

`profiling-inventory.md` documents several captures where `analyse()` "succeeded" but produced no `kernel_details.csv` because the profile window was too short or `FRAMEWORK/torch.op_range` never made it to disk. The skill turns that into `analysis_status == missing_kernel_details` and fails the run. Re-collection is the only fix; offline `analyse()` cannot recover the missing device data.

## Post-stop flush window

The orchestrator sleeps `POST_STOP_FLUSH_SECONDS` (5s) between `/stop_profile` returning and `serve_stop.py` being called. `/stop_profile` should already block until profiler threads quiesce, but historical traces show flush latency on some CANN versions. The window is short enough not to bother humans and long enough to cover known races.

## Local state layout

```
.vaws-local/ascend-profiling-collection/runs/
  <YYYYmmdd_HHMMSS>_<tag>/
    manifest.json
```

One directory per invocation. Nothing else is ever written here. The remote profiling root (`<runtime_dir>/<torch_profiler_dir>`) lives on the container and is referenced from the manifest via `remote_profile_root`.

## Interaction with `remote-code-parity`

Code parity is enforced transitively through `serve_start.py`. The collection skill must not call parity itself. The `.vaws-runtime` preserve carve-out and the `uv pip install --system` fix that profiling needed are owned by the parity skill — see `.agents/skills/remote-code-parity/SKILL.md`.

## Manifest schema versioning

`schema_version` starts at 1. Bump only when a field is renamed or removed. Adding new fields (e.g. richer profiler config knobs) does not require a bump; the analysis skill should treat unknown fields as advisory.
