All parity helpers stream phase progress on `stderr` as `__VAWS_PARITY_PROGRESS__=<json>` and keep the final summary JSON on `stdout`.

# Remote-code-parity command recipes

Prefer the helper scripts in `scripts/` when possible.

## Inspect the current consent state

```bash
python3 .agents/skills/remote-code-parity/scripts/install_consent.py resolve \
  --repo-root . \
  --server-name blue-a \
  --container-identity vaws-blue@/vllm-workspace
```

## Approve the first runtime replacement for one container

```bash
python3 .agents/skills/remote-code-parity/scripts/install_consent.py set \
  --repo-root . \
  --server-name blue-a \
  --container-identity vaws-blue@/vllm-workspace \
  --decision allow \
  --note "approved for first editable install" \
  --approved-by-user
```

## Bulk-approve several containers at once

Input file example:

```json
[
  {
    "server_name": "blue-a",
    "container_identity": "vaws-blue@/vllm-workspace",
    "decision": "allow"
  },
  {
    "server_name": "blue-b",
    "container_identity": "vaws-blue-b@/vllm-workspace",
    "decision": "deny",
    "note": "leave image packages intact"
  }
]
```

Apply:

```bash
python3 .agents/skills/remote-code-parity/scripts/install_consent.py batch-set \
  --repo-root . \
  --input approvals.json \
  --approved-by-user
```

## Inspect the derived sync arguments from inventory

```bash
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py \
  --machine blue-a \
  --print-derived-args
```

## Normal sync against a managed machine

```bash
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py \
  --machine blue-a
```

The runtime-install path sources Ascend env scripts under a `set +u` / `set -u` guard, so first-install parity does not depend on predefining shell-specific variables.
The final verification path is a heredoc-based Python import smoke, so the generated snippet must remain valid Python after shell quoting.
Clean child repos reuse their original `HEAD` commit during snapshotting, so a nested submodule like `csrc/third_party/catlass` does not by itself force a parent reinstall.

## Dry-run sync without remote mutation

```bash
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py \
  --machine blue-a \
  --dry-run
```

## Override runtime root or preserve an extra runtime-private path

```bash
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py \
  --machine blue-a \
  --runtime-root /vllm-workspace \
  --preserve-path model-cache
```

## Low-level sync helper

```bash
python3 .agents/skills/remote-code-parity/scripts/remote_code_parity.py sync \
  --workspace-root . \
  --workspace-id vaws-main \
  --server-name blue-a \
  --container-host 10.0.0.8 \
  --container-port 46001 \
  --container-user root \
  --container-identity vaws-blue@/vllm-workspace \
  --runtime-root /vllm-workspace
```

## Low-level local parity plan

```bash
python3 .agents/skills/remote-code-parity/scripts/remote_code_parity.py plan \
  --workspace-root . \
  --workspace-id vaws-main \
  --server-name blue-a \
  --container-identity vaws-blue@/vllm-workspace \
  --runtime-root /vllm-workspace
```

## Clean old container-local manifests

```bash
python3 .agents/skills/remote-code-parity/scripts/gc_runtime_cache.py \
  --container-host 10.0.0.8 \
  --container-port 46001 \
  --container-user root \
  --workspace-id vaws-main \
  --dry-run
```

## Recommended upper-skill routing rule

When a serving / benchmark / smoke workflow is about to execute remotely:

1. ensure `machine-management` already proved container SSH and recorded the machine in inventory
2. call `remote-code-parity`
3. continue only if `status == ready`

Do **not** continue on `blocked` or `failed`.
