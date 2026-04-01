# Real E2E Acceptance Runbook

Use this runbook to execute the first real-host happy path without exposing raw secrets in tracked docs.

## Pre-stage The Password

The password must be staged outside the agent session:

```bash
export VAWS_SERVER_PASSWORD='<pre-staged outside agent session>'
```

## Run The End-To-End Acceptance Flow

```bash
python /Users/maoxx241/code/vllm-ascend-workspace/tools/vaws.py acceptance run \
  --server-name <server-name> \
  --server-host <server-host> \
  --server-user root \
  --server-password-env VAWS_SERVER_PASSWORD \
  --vllm-upstream-tag 0.18.0 \
  --vllm-ascend-upstream-branch main \
  --benchmark-preset qwen3-35b-tp4
```

Expected outcomes:

- `init` returns success or an idempotent ready result
- runtime verification records the true transport
- code parity reports the remote code matches the local target state
- benchmark writes a remote log and prints extracted markers

## Verify Runtime And Benchmark Separately

```bash
python /Users/maoxx241/code/vllm-ascend-workspace/tools/vaws.py fleet verify <server-name>
python /Users/maoxx241/code/vllm-ascend-workspace/tools/vaws.py benchmark run \
  --server-name <server-name> \
  --preset qwen3-35b-tp4
```

## Extract Only The Benchmark Markers

```bash
ssh root@<server-host> -p <runtime-ssh-port> "bash -ic '
  sed -n \"/MARKDOWN_ROWS_BEGIN/,/MARKDOWN_ROWS_END/p\" /tmp/qwen3-35b-tp4.out
'"
```

Do not stream the full benchmark log by default.
