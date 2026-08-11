# Command Recipes

Inspect the agent identity used for new session attribution:

```bash
python3 .agents/scripts/workspace_identity.py summary
```

Create a session on a ready base machine:

```bash
python3 .agents/skills/session-management/scripts/session_create.py \
  --machine a2-01 \
  --session-id pr123 \
  --devices 0,1
```

Measure the raw base-image bootstrap path without the prepared image cache:

```bash
python3 .agents/skills/session-management/scripts/session_create.py \
  --machine a2-01 \
  --session-id timing-raw \
  --no-worktree \
  --devices 0 \
  --disable-prepared-image-cache
```

Normal session creation keeps the prepared image cache enabled. Set `VAWS_DOCKER_PULL_POLICY=always` only when the image tag must be refreshed before session creation.

Default creation uses SSH-only readiness verification so parallel agents do not all run NPU smoke during setup:

```bash
python3 .agents/skills/session-management/scripts/session_create.py \
  --machine a2-01 \
  --session-id pr123 \
  --devices 0 \
  --verification-mode ssh
```

Use full verification only when the create step itself must validate `torch` / `torch_npu`:

```bash
python3 .agents/skills/session-management/scripts/session_create.py \
  --machine a2-01 \
  --session-id timing-full \
  --no-worktree \
  --devices 0 \
  --verification-mode full
```

Use the session for parity and serving:

```bash
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py --session-id pr123
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --session-id pr123 \
  --model /data/models/Qwen \
  --tp 2
```

Run a session-scoped benchmark:

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --session-id pr123 \
  --model /data/models/Qwen \
  --tp 2
```

Collect and analyze profiling data in the same session container:

```bash
python3 .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py \
  --session-id pr123 \
  --model /data/models/Qwen \
  --served-model-name Qwen \
  --tp 2 \
  --tag pr123_smoke \
  --mode enforce_eager \
  --request-kind text \
  --benchmark-output-tokens 32

python3 .agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py \
  --manifest .vaws-local/ascend-profiling-collection/runs/<timestamp>_pr123_smoke/manifest.json \
  --tag pr123_smoke
```

Collect memory data from a session-scoped service:

```bash
python3 .agents/skills/ascend-memory-profiling/scripts/mem_collect.py \
  --session-id pr123 \
  --attach \
  --tag pr123_memory
```

Remove a session and its resources:

```bash
python3 .agents/skills/session-management/scripts/session_remove.py \
  --session-id pr123 \
  --remove-container \
  --remove-worktree \
  --release-leases
```

## Optional cross-agent NPU queue

Publish a two-device task with a 30-minute estimate and a one-hour queue TTL:

```bash
python3 .agents/skills/session-management/scripts/npu_coordination.py \
  --machine a2-01 submit \
  --task-id pr123-profile \
  --npu-count 2 \
  --queue-ttl-seconds 3600 \
  --estimated-duration-seconds 1800
```

The wrapper reads `agent_id` and `agent_alias` from
`.vaws-local/workspace-identity.json`. Use `--agent-id` or `--agent-alias`
only when an explicit override is required.

Try to acquire the strict FIFO queue head. Save the returned `fence_token` and
`ASCEND_RT_VISIBLE_DEVICES`:

```bash
python3 .agents/skills/session-management/scripts/npu_coordination.py \
  --machine a2-01 acquire \
  --task-id pr123-profile
```

Immediately before launching, recheck real host occupancy:

```bash
python3 .agents/skills/session-management/scripts/npu_coordination.py \
  --machine a2-01 preflight \
  --task-id pr123-profile \
  --fence-token <token>
```

After launching with the returned `ASCEND_RT_VISIBLE_DEVICES`, record the PID
and renew the heartbeat while it runs:

```bash
python3 .agents/skills/session-management/scripts/npu_coordination.py \
  --machine a2-01 activate \
  --task-id pr123-profile \
  --fence-token <token> \
  --pid <pid>

python3 .agents/skills/session-management/scripts/npu_coordination.py \
  --machine a2-01 heartbeat \
  --task-id pr123-profile \
  --fence-token <token>
```

After stopping the workload, release only after two consecutive free probes:

```bash
python3 .agents/skills/session-management/scripts/npu_coordination.py \
  --machine a2-01 release \
  --task-id pr123-profile \
  --fence-token <token>
```

Publish a protected human window and inspect the queue:

```bash
python3 .agents/skills/session-management/scripts/npu_coordination.py \
  --machine a2-01 hold-add \
  --hold-id alice-accuracy \
  --owner human-alice \
  --devices 0,1,2,3 \
  --from 2026-08-11T18:00:00+08:00 \
  --until 2026-08-11T22:00:00+08:00 \
  --reason "manual accuracy run"

python3 .agents/skills/session-management/scripts/npu_coordination.py \
  --machine a2-01 status
```

All commands are optional. Existing task flows remain valid without publishing
or acquiring a cooperative task.
