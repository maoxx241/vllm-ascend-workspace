---
name: ascend-profiling-collection
description: Collect one Ascend torch-profiler case end-to-end on a workspace-managed remote NPU container. Starts a profiled vLLM service, brackets a workload with /start_profile and /stop_profile, runs analyse(), verifies kernel_details.csv landed, and writes a manifest the analysis skill can consume. Use for requests like "采集 profiling", "torch profiler 跑一个 case", "采一份 profile 出来", "采 profiling 给我分析". Do not use for pure performance benchmarking, HBM/memory profiling, or for analysing already-collected profiling data (that is the analysis skill's job).
---

# Ascend Profiling Collection

Collect one torch-profiler case on a workspace-managed remote Ascend NPU container.

This skill is **only** about collection: start a profiled service, bracket a workload with `/start_profile` and `/stop_profile`, run `torch_npu.profiler.profiler.analyse(...)`, verify the device-side data actually landed, and write a manifest. Interpreting the resulting `kernel_details.csv` is a separate concern owned by the analysis skill.

## Use this skill when

- the user asks to collect / capture an Ascend torch-profiler trace for a specific config
- another skill (the analysis skill) needs a fresh profiling root with verified outputs
- the user wants to reproduce an existing root with a new model / mode / TP / DP

## Do not use this skill when

- the task is performance benchmarking only — use `vllm-ascend-benchmark`
- the task is HBM / memory analysis — use `ascend-memory-profiling`
- the task is analysing an already-collected profiling root (no need to re-collect)
- the machine is not yet ready in inventory — use `machine-management`

## Boundary with other skills

| Skill | Owns | This skill uses it for |
| --- | --- | --- |
| `vllm-ascend-serving` | Service lifecycle, `--profiler-config` passthrough | `serve_start.py` / `serve_stop.py` only; serving is **agnostic** to the profiler window |
| `remote-code-parity` | Local-to-container code sync | Implicit — invoked by `serve_start.py` |
| `vllm-ascend-benchmark` | `vllm bench serve` performance numbers | Not used; benchmark skill must not learn the profiler control plane |
| `ascend-memory-profiling` | HBM attribution via msprof | Independent; not invoked |

`/start_profile` and `/stop_profile` exist *because of* profiling, so the control-plane client lives here, not in `vllm-ascend-serving`.

## Critical rules

- The serving skill must remain profiling-agnostic. Never push profiler-window control or `analyse()` invocation into it.
- `--profiler-config` is the only profiling-related thing the serving skill knows about, and only because vLLM accepts it as an opaque blob.
- Run profiling **inside the remote container**. Never copy raw `*_ascend_pt` directories back to the local Mac.
- Hard-fail when any rank's `kernel_details.csv` is missing after `analyse()` — see "Failure policy" below.
- Progress on `stderr` as `__VAWS_PROFILING_COLLECTION_PROGRESS__=<json>`. Final manifest on `stdout` as one JSON object.
- Local state lives **only** under `.vaws-local/ascend-profiling-collection/runs/`.

## Public entry point

```bash
python3 .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py \
  --machine <alias-or-ip> \
  --model <remote-weight-path> \
  --served-model-name <name> \
  --tp <N> \
  --tag <stable-id> \
  --mode {enforce_eager|full_decode_only|piecewise_graph} \
  --request-kind {text|vl} \
  --benchmark-output-tokens <N> \
  [--dp <N>] \
  [--enable-expert-parallel] \
  [--speculative-tokens <N>] [--speculative-method <name>] \
  [--gpu-memory-utilization <f>] \
  [--max-model-len <N>] [--max-num-seqs <N>] [--max-num-batched-tokens <N>] \
  [--api-server-count <N>] \
  [--prompt-tokens <N>] [--followup-output-tokens <N>] \
  [--benchmark-total-requests <N>] [--benchmark-concurrency <N>] \
  [--request-timeout <s>] [--profile-control-timeout <s>] \
  [--torch-profiler-dir <relpath>] [--torch-profiler-with-stack] \
  [--image-path <local-path>] [--image-height <px>] \
  [--skip-parity]
```

### Required parameters and why

The script intentionally has no Qwen-specific defaults. The agent must always pass:

| Required arg | Why |
| --- | --- |
| `--machine` | Target container; resolved through inventory |
| `--model` / `--served-model-name` | Different cases need different models, no safe default |
| `--tp` | Hardware shape; never assume it |
| `--mode` | The profile is meaningless without recording which graph mode produced it |
| `--request-kind` | Determines payload assembly (text vs VL) |
| `--benchmark-output-tokens` | Decode length is the dominant knob for what the trace looks like |
| `--tag` | Stable identifier folded into the run-dir name and manifest |

`--speculative-tokens 0` (the default) means "do not pass `--speculative-config` at all". Set to a positive integer to enable MTP/Eagle.

## Auxiliary entry points

The agent can call these directly if it already has a service running and only wants to flip the profiler window or re-run `analyse()` on an existing root.

### Flip the profiler window

```bash
# Start a profile window on a service that the serving skill already launched
python3 .agents/skills/ascend-profiling-collection/scripts/profile_control.py \
  --machine <alias> --action start_profile [--timeout 900]

# Close it
python3 .agents/skills/ascend-profiling-collection/scripts/profile_control.py \
  --machine <alias> --action stop_profile [--timeout 900]
```

The script reads the service port from `.vaws-local/serving/<alias>.json`. A service must be running.

### Re-run `analyse()` on an existing root

```bash
python3 .agents/skills/ascend-profiling-collection/scripts/run_remote_analyse.py \
  --machine <alias> --profile-root <remote-path>
```

Discovers every `*_ascend_pt` under `--profile-root`, runs `torch_npu.profiler.profiler.analyse()` on each, and verifies that `ASCEND_PROFILER_OUTPUT/kernel_details.csv` and `trace_view.json` landed. Exits non-zero if any rank is incomplete.

## Workflow

1. **Resolve machine** via inventory; container endpoint comes from `machine-management`.
2. **Build serving args** — encode `--profiler-config` (always written) and the chosen graph mode.
3. **Start service** by shelling out to `serve_start.py`. Parity sync is automatic via the serving skill.
4. **Open SSH tunnel** to the service port so workload requests can be assembled locally (multimodal payloads need local image encoding).
5. **POST `/start_profile`** with the long control-plane timeout.
6. **Send benchmark wave** (concurrent chat-completions) followed by **one follow-up tail request**. The follow-up is intentionally short to capture a clean steady-state step.
7. **POST `/stop_profile`**.
8. **Stop service** by shelling out to `serve_stop.py`.
9. **Discover and analyse** every `*_ascend_pt` under `<runtime_dir>/<torch_profiler_dir>` via `run_remote_analyse.py`.
10. **Verify outputs** per rank; classify each as `ok | partial | missing_kernel_details`.
11. **Write manifest** to `.vaws-local/ascend-profiling-collection/runs/<timestamp>_<tag>/manifest.json`.

## Failure policy

Accuracy beats coverage. The script exits non-zero (status `failed`) when:

- the service did not become `ready`
- `/start_profile` or `/stop_profile` returned non-2xx
- any benchmark request raised before the profile window closed (recorded but not by itself fatal — agent decides)
- any rank's `kernel_details.csv` is missing after `analyse()` — `analysis_status == missing_kernel_details`

The third condition is the canonical "device-side data did not land" failure documented in `doc/profiling-inventory.md`. Treat it as **re-collect required**, not as something the analysis skill can recover from.

If the orchestrator fails after `serve_start`, it always tries to stop the service (graceful, then `--force`) so no orphan vLLM process is left behind.

## Manifest schema

The manifest is the input contract for the analysis skill. Important fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Bumped when fields are renamed or removed |
| `tag`, `started_at`, `completed_at` / `failed_at` | Run identity |
| `machine`, `model`, `served_model_name`, `tp`, `dp` | Hardware / model identity |
| `mode`, `speculative_tokens`, `speculative_method`, `enable_expert_parallel`, `api_server_count` | What was profiled |
| `request_kind`, `prompt_tokens`, `benchmark_output_tokens`, `followup_output_tokens`, `benchmark_total_requests`, `benchmark_concurrency` | What workload produced the trace |
| `torch_profiler_with_stack`, `torch_profiler_dir` | Profiler depth and on-disk location |
| `serve_args` | Exact `serve_start.py` argv (audit trail) |
| `service_result`, `start_profile`, `stop_profile`, `stop_result` | Sub-call outputs |
| `benchmark_results`, `followup_result` | Per-request status / latency / response body |
| `remote_profile_root` | Path the analysis skill passes to its `analyze.py` |
| `remote_profile_dirs` | Per-rank `{path, outputs, analysis_status}` |
| `analysis_status` | `ok` / `partial` / `missing_kernel_details` — the hard gate |
| `status` | `ok` / `failed` |
| `error` | Set when `status == failed` |

## Reference files

- `references/command-recipes.md` — common `collect_torch_profile_case.py` invocations
- `references/behavior.md` — control-plane timing, mode mapping, image encoding caveats
- `references/acceptance.md` — manual checks before marking a collection good
