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

## Set local sync and approve first editable replacement

Use this when the user explicitly says to run the local workspace code, replace
the image `vllm` / `vllm-ascend`, or "用本地的 vllm 和 vllm-ascend 替换".
This is the preferred first-use command because it writes sync mode and
first-install consent atomically.

```bash
python3 .agents/skills/remote-code-parity/scripts/install_consent.py set-sync-mode \
  --server-name blue-a \
  --container-identity vaws-blue@/vllm-workspace \
  --sync-mode local \
  --allow-first-install \
  --note "use local workspace packages" \
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
It also scans versioned CANN paths such as `/usr/local/Ascend/cann-9.0.0/set_env.sh`, so A3 images that do not expose only `/usr/local/Ascend/ascend-toolkit/set_env.sh` still get HCCL/CANN libraries.
Package installation is pip-only and uses the single A3-tested HuaweiCloud index. Do not install or invoke `uv`, probe mirror candidates, or configure default extra indexes in the parity path.
Editable installs use `--no-deps`; `vllm-ascend/requirements.txt` owns dependency versions so `vllm` cannot upgrade `numpy` beyond the CANN-compatible stack. The runtime preamble also exports bounded build parallelism through `MAX_JOBS` and `CMAKE_BUILD_PARALLEL_LEVEL` (`min(available CPUs, 128)` by default), which keeps `vllm-ascend` nested CMake builds from falling back to serial protobuf compilation.
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

The editable install path carries only cache and compile knobs; package source selection stays fixed to HuaweiCloud:

```bash
VAWS_BUILD_JOBS=64 \
MAX_JOBS=64 \
CMAKE_BUILD_PARALLEL_LEVEL=64 \
CMAKE_BUILD_TYPE=Release \
PIP_CACHE_DIR=/root/.cache/pip \
FETCHCONTENT_BASE_DIR=/root/.cache/vaws/fetchcontent \
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py \
  --machine blue-a \
  --force-reinstall
```

Defaults when these variables are unset: `VAWS_BUILD_JOBS=min(available CPUs, 128)`, `MAX_JOBS=$VAWS_BUILD_JOBS`, `CMAKE_BUILD_PARALLEL_LEVEL=$VAWS_BUILD_JOBS`, `CMAKE_BUILD_TYPE=Release`, persistent pip and `FetchContent` cache roots under `/root/.cache`, and the single HuaweiCloud pip index. `VAWS_COMPILE_CUSTOM_KERNELS=0` is available only for deliberate unit-test-style checks; do not use it for real serving or benchmark validation.

The cache/compile values shown above are exported into the remote install shell by `remote-code-parity`; they do not depend on OpenSSH `SendEnv` / `AcceptEnv`. Set `VAWS_SOC_VERSION=<soc>` to force `SOC_VERSION` when auto-detection is not enough. Editable installs always use `--no-deps`; dependency changes are handled by the explicit `vllm-ascend/requirements.txt` step, then verified without repair reinstall.

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
