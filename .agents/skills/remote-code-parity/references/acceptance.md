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
- normal outcomes are reported as compact JSON with `status` equal to `ready`, `blocked`, `failed`, or `dry-run`
- phase progress is emitted on `stderr` as `__VAWS_PARITY_PROGRESS__=<json>` while the final summary stays on `stdout`
- long runtime-install waits remain attributable because uninstall, requirements install, editable install, import verification, and marker write each emit their own progress phase

### Repo graph and snapshotting

- the workspace root, `vllm/`, and `vllm-ascend/` all participate in parity
- synthetic snapshots can represent dirty working trees without forcing a real commit
- when `vllm` or `vllm-ascend` changed locally, the workspace-root synthetic snapshot also changes because the gitlinks are rewritten to the synthetic child commits
- ignored files are not added to the snapshot by default
- denylisted files such as `.vaws-local/*`, `.env*`, and local cache directories are excluded
- if a required submodule path exists but is not populated, the skill fails closed with a clear error instead of producing a traceback-heavy Git failure

### Container-only cache path

- the normal sync path does not require host storage, host lock directories, or `docker inspect`
- container-local bare mirrors are populated directly through container SSH
- advertised branch refs are published inside the mirror so synthetic child commits are fetchable through ordinary Git paths
- the normal agent-facing entrypoint can resolve the target from machine inventory through `parity_sync.py`
- the skill does not create or reuse a flat shared host path such as `/home/vaws`

### Consent and first install

- first sync on a fresh container without recorded approval ends with `status == blocked`
- consent writes require explicit `--approved-by-user`
- first sync on a fresh container with `allow` recorded proceeds to uninstall image-provided packages best-effort, remove only `vllm` and `vllm-ascend`, and perform editable installs
- later syncs on the same logical container identity do not ask again unless the marker or identity changed
- batch approval supports mixed decisions across different servers or containers

### Runtime materialization and proof

- `/vllm-workspace` is updated in place instead of being replaced wholesale
- nested repos are materialized explicitly instead of relying on `git submodule update` for synthetic child commits
- runtime-private paths such as `Mooncake` survive root cleanups
- final `git rev-parse HEAD` values inside the container match the synthetic snapshot commits exactly
- reinstall runs only when the trigger matrix says it should, except for the mandatory first approved replacement on a fresh container
- a successful run ends with `status == ready`
- runtime install uses dynamic Python discovery plus the Ascend driver `LD_LIBRARY_PATH` preamble instead of one hard-coded Python patch path
- packaging-metadata failures from stale image toolchains trigger one bounded packaging-stack refresh / retry before the skill reports `failed`

## Regression checklist from this patch

These specific mistakes should no longer be part of the normal path:

- Python helper files should not start with an extra leading backslash before the shebang
- the normal path should not depend on host `storage_root` arguments
- repeated `plan` or `sync` runs should not accumulate unbounded local temporary parity refs
- temporary parity index files should be cleaned up after snapshot construction
- missing or unpopulated required submodules should return a compact failure payload instead of a raw Git traceback
- the main snapshot path should not force ignored files into the synthetic snapshot
- root cleanups inside `/vllm-workspace` should not delete `Mooncake`

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
