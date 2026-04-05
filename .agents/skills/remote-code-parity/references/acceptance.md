# Remote-code-parity acceptance criteria

## Trigger examples

These should trigger `remote-code-parity` directly or as an automatic internal step:

- “同步远端代码后再拉服务，包含我本地没 commit 的改动。”
- “在 ready 的蓝区机器上启动 benchmark 前，确保跑的是我本地最新修改。”
- “这台机器已经 ready 了，先把我本地 workspace 的最新改动同步进去再跑 smoke。”
- “这个容器是新的，先批量确认允许第一次 editable install，再同步代码。”

## Non-trigger examples

These should not trigger `remote-code-parity` unless remote code parity is the obvious blocker:

- “帮我配置一台远端 NPU 机器。”
- “修一下这台机器的 SSH 和容器 ready。”
- “只帮我把 remotes 配好并初始化 submodules。”
- “把我本地这几个改动 commit 然后推到 GitHub。”
- “解释一下这段代码。”

## Success criteria

### Universal

- the skill treats the local working tree as the source of truth, including committed, staged, unstaged, and untracked files
- the skill does not require the user to commit or push before parity
- the skill does not use `scp`, `sftp`, `rsync`, `sshpass`, or `expect`
- the skill does not require GitHub credentials on the remote host or in the container
- the skill keeps local runtime state only under `.vaws-local/remote-code-parity/`
- normal outcomes are reported as compact JSON with `status` equal to `ready`, `blocked`, or `failed`

### Repo graph and snapshotting

- the workspace root, `vllm/`, and `vllm-ascend/` all participate in parity
- synthetic snapshots can represent dirty working trees without forcing a real commit
- when `vllm` or `vllm-ascend` changed locally, the workspace-root synthetic snapshot also changes because the gitlinks are rewritten to the synthetic child commits
- ignored-but-allowed files can be included in the snapshot
- denylisted files such as `.vaws-local/*`, `.env*`, and local cache directories are excluded
- if a required submodule path exists but is not populated, the skill fails closed with a clear error instead of producing a traceback-heavy Git failure

### Consent and first install

- first sync on a fresh container without recorded approval ends with `status == blocked`
- first sync on a fresh container with `allow` recorded proceeds to uninstall image-provided packages best-effort and perform editable installs
- later syncs on the same container do not ask again unless the container identity changed
- batch approval supports mixed decisions across different servers or containers
- a rebuilt or recreated container with a new identity requires consent again

### Storage-root selection

- if local state already records a validated `storage_root` for the server, the skill reuses it
- if no `storage_root` is known yet, the skill can probe candidate paths and select the first acceptable one
- shared filesystems such as NFS, CIFS, SSHFS, Lustre, Ceph, and GlusterFS are rejected
- paths below 512 MiB free space fail closed before mirror publication
- first editable reinstall below 4 GiB free space fails closed before runtime mutation

### Runtime materialization and proof

- the host-local bare mirrors are updated before the container fetches them
- the runtime tree inside the container is forced to the synthetic snapshot commits rather than to a branch tip from GitHub
- final `git rev-parse HEAD` values inside the container match the synthetic snapshot commits exactly
- reinstall runs only when the trigger matrix says it should, except for the mandatory first approved replacement on a fresh container
- a successful run ends with `status == ready`

## Regression checklist from this patch

These specific mistakes should no longer be part of the normal path:

- Python helper files should not start with an extra leading backslash before the shebang
- `command-recipes.md` should not tell the agent to pass unsupported `plan` arguments such as host or container SSH fields
- repeated `plan` or `sync` runs should not accumulate unbounded local temporary parity refs
- temporary parity index files should be cleaned up after snapshot construction
- missing or unpopulated required submodules should return a compact failure payload instead of a raw `git add` traceback

## Manual regression checklist

Review these files together after every substantial skill edit:

- `.agents/skills/remote-code-parity/SKILL.md`
- `.agents/skills/remote-code-parity/references/behavior.md`
- `.agents/skills/remote-code-parity/references/command-recipes.md`
- `.agents/skills/remote-code-parity/references/acceptance.md`
- `.agents/skills/remote-code-parity/scripts/common.py`
- `.agents/skills/remote-code-parity/scripts/remote_code_parity.py`
- `.agents/skills/remote-code-parity/scripts/install_consent.py`
- `.agents/skills/remote-code-parity/scripts/gc_runtime_cache.py`
- `AGENTS.md`
- `.agents/README.md`
- `README.md`
