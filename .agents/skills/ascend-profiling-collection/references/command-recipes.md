# Command Recipes

These are the expected shapes the agent should call. Every invocation is a single shot — there is no "warm collection" mode. Collection is session-scoped: run from inside the session worktree and the session auto-resolves from the `.vaws-local/current-session.json` binding, so no target arg is needed.

## Minimal text case in eager mode

Run from inside the session worktree (target auto-resolved):

```bash
python3 .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py \
  --model /home/weights/Qwen3-8B \
  --served-model-name Qwen3-8B \
  --tp 1 \
  --tag qwen3_8b_eager_text \
  --mode enforce_eager \
  --request-kind text \
  --benchmark-output-tokens 64
```

To target a session explicitly (e.g. from outside its worktree), add `--session-id`:

```bash
python3 .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py \
  --session-id pr123 \
  --model /home/weights/Qwen3-8B \
  --served-model-name Qwen3-8B \
  --tp 1 \
  --tag qwen3_8b_eager_text \
  --mode enforce_eager \
  --request-kind text \
  --benchmark-output-tokens 64
```

## Full-decode-only graph mode with MTP=3 speculative decoding

```bash
python3 .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py \
  --model /home/weights/Qwen3.5-35B-A3B \
  --served-model-name Qwen3.5-35B-A3B \
  --tp 4 --dp 2 --enable-expert-parallel \
  --tag qwen35_35b_dp2tp4_fdo_mtp3 \
  --mode full_decode_only \
  --speculative-tokens 3 --speculative-method qwen3_5_mtp \
  --request-kind text \
  --benchmark-output-tokens 256 \
  --benchmark-total-requests 10 --benchmark-concurrency 5 \
  --max-num-seqs 2 --max-num-batched-tokens 1024
```

## Piecewise graph mode with VL workload

```bash
python3 .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py \
  --model /home/weights/Qwen3.5-VL \
  --served-model-name Qwen3.5-VL \
  --tp 4 \
  --tag qwen35_vl_piecewise_vl \
  --mode piecewise_graph \
  --request-kind vl \
  --benchmark-output-tokens 64 \
  --image-height 480
```

`--image-path` defaults to `vllm-ascend/tests/e2e/310p/data/qwen.png` so VL collection works without extra args. Override only if the agent wants a different test image.

## Long control-plane timeout for many ranks

```bash
python3 .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py \
  --model /home/weights/DeepSeek-V3 \
  --served-model-name DeepSeek-V3 \
  --tp 8 \
  --tag dsv3_tp8_fdo \
  --mode full_decode_only \
  --request-kind text \
  --benchmark-output-tokens 64 \
  --profile-control-timeout 1800 --request-timeout 1800
```

Multi-rank torch profiler setup/finalization can take several minutes. Bump `--profile-control-timeout` whenever TP × DP grows.

## Re-analyse an already-collected root

Use this when a previous collection ran but `analyse()` was skipped or the agent wants to re-verify outputs without re-collecting.

```bash
python3 .agents/skills/ascend-profiling-collection/scripts/run_remote_analyse.py \
  --profile-root /vllm-workspace/.vaws-runtime/serving/<timestamp>/vllm_profile \
  --expected-ranks 8
```

Run from inside the session worktree (target auto-resolved), or add `--session-id <id>` / `--session-file <path>` to target a session explicitly.

Exit 0 means every rank produced `kernel_details.csv` and `trace_view.json` AND the directory count matched `--expected-ranks` (typically `tp * (dp or 1)`). Non-zero means re-collection is needed. Always pass `--expected-ranks` against fresh roots — without it a partial capture where some ranks never produced a directory looks "clean".

## Manually flip the profiler window on a service the agent already started

For debugging the control plane in isolation. The serving skill must have launched a service with a `--profiler-config` argument first.

```bash
python3 .agents/skills/ascend-profiling-collection/scripts/profile_control.py \
  --action start_profile --timeout 900

# ... agent sends workload via curl / openai client / etc ...

python3 .agents/skills/ascend-profiling-collection/scripts/profile_control.py \
  --action stop_profile --timeout 900
```

The script reads the service port from `.vaws-local/sessions/<id>/serving.json` for the resolved session (auto-resolved from the worktree binding, or named by `--session-id` / `--session-file`).

## What NOT to do

- Do not run `collect_torch_profile_case.py` against a session whose container is not yet ready. Create the session with `session-management` (which bootstraps via `machine-management`) first.
- Do not pass `--profiler-config` through `--serve-args` of `vllm-ascend-benchmark` to "sneak" profiling into a benchmark run; benchmarks should not own profiler windows.
- Do not call `torch_npu.profiler.profiler.analyse()` from the local Mac. The collection / re-analyse scripts run it inside the container.
- Do not share state with `.vaws-local/service-torch-profiler/` (the old prototype location). The new skill writes only to `.vaws-local/ascend-profiling-collection/runs/`.
