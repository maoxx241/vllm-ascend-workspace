All parity helpers stream phase progress on `stderr` as `__VAWS_PARITY_PROGRESS__=<json>` and keep the final summary JSON on `stdout`.

Remote toolbox sync planning:

```bash
python3 .agents/scripts/remote_sync_plan.py --session-id <id> --mode source-only
python3 .agents/scripts/remote_sync_plan.py --session-id <id> --mode install
```

Remote toolbox sync apply without install/rebuild:

```bash
python3 .agents/scripts/remote_sync_apply.py --session-id <id> --mode source-only
python3 .agents/scripts/remote_sync_apply.py --session-id <id> --mode materialize
```

# Remote-code-parity command recipes

Prefer the helper scripts in `scripts/` when possible.

## Check sync mode for a container

```bash
python3 .agents/skills/remote-code-parity/scripts/install_consent.py resolve-sync-mode \
  --server-name blue-a \
  --container-identity vaws-blue@/vllm-workspace
```

## Set sync mode to use image-provided packages (skip parity)

```bash
python3 .agents/skills/remote-code-parity/scripts/install_consent.py set-sync-mode \
  --server-name blue-a \
  --container-identity vaws-blue@/vllm-workspace \
  --sync-mode image \
  --approved-by-user
```

## Set sync mode to sync local code

```bash
python3 .agents/skills/remote-code-parity/scripts/install_consent.py set-sync-mode \
  --server-name blue-a \
  --container-identity vaws-blue@/vllm-workspace \
  --sync-mode local \
  --approved-by-user
```

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

## Normal sync against an isolated session

```bash
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py \
  --session-id pr123
```

This syncs the session worktree to the session container and uses `workspace_id=pr123` unless explicitly overridden.

The runtime-install path sources Ascend env scripts under a `set +u` / `set -u` guard, so first-install parity does not depend on predefining shell-specific variables.
The final verification path is a heredoc-based Python import smoke, so the generated snippet must remain valid Python after shell quoting.
Synthetic commits are deterministic parentless tree snapshots. Clean child repos still avoid parent reinstall churn because transport-only child gitlink paths are filtered out of parent `changed_paths`.

## Force full reinstall without code changes

```bash
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py \
  --machine blue-a \
  --force-reinstall
```

Unconditionally reinstalls both `vllm` and `vllm-ascend` even when no files changed. Useful for recovering from a broken editable install or validating the install pipeline.

## Runtime install cache / compile knobs

The editable install path carries portable defaults from the `vllm-ascend` CI compile jobs:

```bash
VAWS_MAX_JOBS=8 \
VAWS_UV_BOOTSTRAP_TIMEOUT=120 \
VAWS_UV_INSTALL_TIMEOUT=300 \
VAWS_DISABLE_UV=0 \
VAWS_INSTALL_DEPS=0 \
FETCHCONTENT_BASE_DIR=/root/.cache/vaws/fetchcontent \
VAWS_PIP_INDEX_URL=http://near-cache.example/pypi/simple \
VAWS_PIP_TRUSTED_HOST=near-cache.example \
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py \
  --machine blue-a \
  --force-reinstall
```

Defaults are conservative when these variables are unset: `MAX_JOBS=4`, `CMAKE_BUILD_TYPE=Release`, persistent pip / uv / `FetchContent` cache roots under `/root/.cache`, public mirror fallback, and the Ascend PyPI repository as an extra index. `VAWS_COMPILE_CUSTOM_KERNELS=0` is available only for deliberate unit-test-style checks; do not use it for real serving or benchmark validation.

The `VAWS_*` values shown above are exported into the remote install shell by `remote-code-parity`; they do not depend on OpenSSH `SendEnv` / `AcceptEnv`. The default Ascend extra index is scoped to `vllm-ascend` requirements/editable install steps. Set `VAWS_ASCEND_PIP_EXTRA_INDEX_URL=` to disable that default, or set `VAWS_PIP_EXTRA_INDEX_URL` when all package steps should use a custom extra index. Set `VAWS_SOC_VERSION=<soc>` to force `SOC_VERSION` when `npu-smi` auto-detection is not enough. `VAWS_UV_BOOTSTRAP_TIMEOUT` and `VAWS_UV_INSTALL_TIMEOUT` bound uv attempts before continuing with mirror/pip fallback; set `VAWS_DISABLE_UV=1` for pip-only validation. Editable installs default to `--no-deps` against the paired image; set `VAWS_INSTALL_DEPS=1` when validating dependency changes.

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
