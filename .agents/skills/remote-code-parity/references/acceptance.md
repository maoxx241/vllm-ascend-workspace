# Remote-code-parity acceptance criteria

## Trigger examples

These should trigger `remote-code-parity` directly or as an automatic internal step:

- “同步远端代码后再拉服务，包含我本地没 commit 的改动。”
- “在 ready 的蓝区机器上启动 benchmark 前，确保跑的是我本地最新修改。”
- “这台机器已经 ready 了，先把我本地 workspace 的最新改动同步进去再跑 smoke。”
- “这个容器是新的，先确认允许第一次 editable install，再同步代码。”

## Non-trigger examples

These should not trigger `remote-code-parity` unless remote code parity is the obvious blocker:

- “帮我配置一台远端 NPU 机器。”
- “修一下这台机器的 SSH 和容器 ready。”
- “只帮我把 remotes 配好并初始化 submodules。”
- “把我本地这几个改动 commit 然后推到 GitHub。”
- “解释一下这段代码。”

## Success criteria

### Universal

- the skill treats the local working tree as the source of truth, including committed, staged, unstaged, and untracked **non-ignored** files
- the skill does not require the user to commit or push before parity
- the skill does not use `scp`, `sftp`, `rsync`, `sshpass`, or `expect`
- the skill does not require GitHub credentials on the host or in the container
- the skill keeps local runtime state only under `.vaws-local/remote-code-parity/`
- session mode resolves the source worktree and container endpoint from `.vaws-local/sessions/<session-id>/session.json`
- normal outcomes are reported as compact JSON with `status` equal to `ready`, `blocked`, `failed`, or `dry-run`
- phase progress is emitted on `stderr` as `__VAWS_PARITY_PROGRESS__=<json>` while the final summary stays on `stdout`
- `remote_sync_plan.py --mode source-only` reports that install/rebuild will not run
- `remote_sync_apply.py --mode source-only` completes without invoking runtime install phases
- `remote_sync_plan.py --mode install` reports install/rebuild reasons and current consent state
- long runtime-install waits remain attributable because uninstall, requirements install, editable install, import verification, and marker write each emit their own progress phase
- whitelisted local env overrides such as `VAWS_MAX_JOBS`, `VAWS_PIP_INDEX_URL`, and `VAWS_SOC_VERSION` are explicitly exported inside the remote install shell instead of relying on SSH env forwarding
- runtime install can opt into a caller-provided near-cache index via `VAWS_PIP_INDEX_URL`, then keeps pip mirror fallback in the order Tsinghua -> Aliyun -> PyPI instead of hard-coding only the public index
- runtime install adds the Ascend PyPI repository as a default extra index only for `vllm-ascend` requirements/editable install steps and allows the caller to override it through `VAWS_PIP_EXTRA_INDEX_URL` or disable the default by exporting an empty `VAWS_ASCEND_PIP_EXTRA_INDEX_URL`
- runtime install exports persistent pip, uv, and CMake `FetchContent` cache roots outside the synced repos so normal materialization cleanups do not delete dependency caches
- runtime install wraps uv bootstrap with progress and a per-mirror timeout controlled by `VAWS_UV_BOOTSTRAP_TIMEOUT`, then continues with pip fallback when uv cannot be installed quickly
- runtime install bounds uv package install attempts with `VAWS_UV_INSTALL_TIMEOUT` and supports `VAWS_DISABLE_UV=1` for pip-only validation when uv resolution stalls
- runtime editable installs default to `--no-deps` against the paired image and skip ordinary `vllm-ascend` requirements reinstall; dependency resolution is re-enabled for dependency-file changes, HEAD drift, verify-deps repair, or `VAWS_INSTALL_DEPS=1`
- paired-image `torch_npu` is verified by public-version compatibility and real import smoke instead of forcing a `torch-npu` wheel reinstall during verify-deps repair
- runtime install defaults to `MAX_JOBS=4` and `CMAKE_BUILD_TYPE=Release`, while allowing `VAWS_MAX_JOBS`, `MAX_JOBS`, and `CMAKE_BUILD_TYPE` to override the compile profile
- runtime install does not set `COMPILE_CUSTOM_KERNELS=0` unless the caller explicitly exports `VAWS_COMPILE_CUSTOM_KERNELS=0`
- runtime install records its effective cache/compile/index env in the manifest, final summary, and runtime state with URL userinfo redacted
- runtime install forwards `VAWS_SOC_VERSION` as `SOC_VERSION` and emits an `npu-smi` probe before building `vllm-ascend` when `npu-smi` is available

### Repo graph and snapshotting

- the workspace root, `vllm/`, and `vllm-ascend/` all participate in parity
- synthetic snapshots can represent dirty working trees without forcing a real commit
- when `vllm` or `vllm-ascend` changed locally, the workspace-root synthetic snapshot also changes because the gitlinks are rewritten to the synthetic child commits
- synthetic snapshot commits are parentless, so first mirror hydration transfers the snapshot tree instead of the full upstream history
- parentless snapshot mirror hydration uses Git bundle import, so it does not depend on remote receive-pack negotiation or remote base history that may not exist
- repeated snapshots of the same workspace tree produce the same synthetic commit ids, enabling the no-change fast path
- when a nested child repo has no logical changes, the parent repo does not report a reinstall-relevant `changed_paths` entry just because the child was represented by a parentless transport commit
- ignored files are not added to the snapshot by default
- denylisted files such as `.vaws-local/*`, `.env*`, and local cache directories are excluded
- if a required submodule path exists but is not populated, the skill fails closed with a clear error instead of producing a traceback-heavy Git failure

### Container-only cache path

- the normal sync path does not require host storage, host lock directories, or `docker inspect`
- container-local bare mirrors are populated by SSH-streamed Git bundles, without requiring GitHub credentials or host storage
- advertised branch refs are published inside the mirror so synthetic child commits are fetchable through ordinary Git paths
- stale container lock directories are eventually recovered instead of permanently blocking later parity attempts
- failed or timed-out mirror hydration does not leave matching legacy `git-receive-pack` process trees or partial repo mirrors blocking later retries
- the normal agent-facing entrypoint can resolve the target from machine inventory through `parity_sync.py`
- the normal agent-facing entrypoint can also resolve a session target through `parity_sync.py --session-id <id>`
- the skill does not create or reuse a flat shared host path such as `/home/vaws`

### Sync mode gate

- when `sync_mode` is `unset`, the agent proactively asks the user before running parity
- when `sync_mode` is `image`, `parity_sync.py` returns `status: skipped` without any remote operations
- when `sync_mode` is `local`, the full parity flow proceeds normally
- `--force-reinstall` overrides `image` mode and forces a full sync + reinstall
- the user can switch sync mode at any time

### Consent and first install

- first sync on a fresh container without recorded approval ends with `status == blocked`
- consent writes require explicit `--approved-by-user`
- consent and runtime-state writes are atomic / lock-protected so concurrent sync wrappers do not overwrite each other
- first sync on a fresh container with `allow` recorded proceeds to uninstall image-provided packages best-effort, remove only `vllm` and `vllm-ascend`, and perform editable installs
- later syncs on the same logical container identity do not ask again unless the marker or identity changed
- batch approval supports mixed decisions across different servers or containers

### Runtime materialization and proof

- final verification performs real imports for `vllm`, `vllm_ascend`, and `torch_npu` in the prepared runtime environment

- `/vllm-workspace` is updated in place instead of being replaced wholesale
- nested repos are materialized explicitly instead of relying on `git submodule update` for synthetic child commits
- runtime-private paths such as `Mooncake` and `.vaws-runtime` survive root cleanups
- final `git rev-parse HEAD` values inside the container match the synthetic snapshot commits exactly
- reinstall runs only when the trigger matrix says it should, except for the mandatory first approved replacement on a fresh container
- switching a submodule to a different commit (e.g. `git checkout v0.8.0` in `vllm/`) triggers reinstall via HEAD-based commit drift detection (comparing real HEAD, not synthetic snapshot commit), even when the new checkout is clean
- pure Python file edits inside a submodule do not trigger reinstall even though the synthetic snapshot commit changes
- vllm reinstall cascades to vllm-ascend reinstall because of the runtime dependency
- uninstall only removes packages that will be reinstalled; changing only vllm-ascend does not uninstall vllm
- first install does not run the reinstall-branch uninstall step because `first_install_prepare_script` already handled it
- when nothing changed since last sync (snapshot commits == last_snapshot_commits, no reinstall needed), the sync verifies the container with a single SSH call and returns `status == ready` immediately
- `--force-reinstall` unconditionally triggers full reinstall of both vllm and vllm-ascend even when no files changed
- a successful run ends with `status == ready`
- runtime install uses dynamic Python discovery plus a shell-safe env preamble and guarded env-script sourcing instead of one hard-coded Python patch path
- packaging-metadata failures from stale image toolchains trigger one bounded packaging-stack refresh / retry before the skill reports `failed`

## Regression checklist from this patch

These specific mistakes should no longer be part of the normal path:

- Python helper files should not start with an extra leading backslash before the shebang
- the normal path should not depend on host `storage_root` arguments
- repeated `plan` or `sync` runs should not accumulate unbounded local temporary parity refs
- temporary parity index files should be cleaned up after snapshot construction
- missing or unpopulated required submodules should return a compact failure payload instead of a raw Git traceback
- the main snapshot path should not force ignored files into the synthetic snapshot
- root cleanups inside `/vllm-workspace` should not delete `Mooncake` or `.vaws-runtime`
- runtime-install should not fail closed just because Ascend env scripts reference shell-specific or otherwise unset variables while being sourced
- the final heredoc-based import smoke should not fail with a local quoting `SyntaxError`
- clean nested submodules should not force parent `reinstall_vllm*` decisions through parentless transport gitlink churn alone
- changing only vllm-ascend files should not uninstall or break the vllm editable install
- switching vllm to a different commit should trigger both vllm and vllm-ascend reinstall
- consecutive syncs with no local changes should take the fast path and skip mirror hydration, materialize, and manifest upload

## Manual regression checklist

Review these files together after every substantial skill edit:

- `.agents/skills/remote-code-parity/SKILL.md`
- `.agents/skills/remote-code-parity/references/behavior.md`
- `.agents/skills/remote-code-parity/references/command-recipes.md`
- `.agents/skills/remote-code-parity/references/acceptance.md`
- `.agents/skills/remote-code-parity/scripts/common.py`
- `.agents/skills/remote-code-parity/scripts/remote_code_parity.py`
- `.agents/skills/remote-code-parity/scripts/parity_sync.py`
- `.agents/skills/remote-code-parity/scripts/install_consent.py`
- `.agents/skills/remote-code-parity/scripts/gc_runtime_cache.py`
- `AGENTS.md`
- `.agents/README.md`
- `README.md`
