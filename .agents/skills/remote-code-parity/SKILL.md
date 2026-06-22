---
name: remote-code-parity
description: Ensure a ready remote runtime runs the exact current local workspace state before any remote smoke, service launch, or benchmark. Use automatically immediately before remote execution when direct local -> container SSH already works and local uncommitted changes must be reflected remotely. Do not use for initial machine attach, generic Git topology work, or unrelated local-only coding.
---

# Remote Code Parity

Keep a **ready** remote runtime in exact code parity with the local `vllm-ascend-workspace` checkout.

## Use this skill when

- a remote smoke, service launch, or benchmark is about to start
- `machine-management` already proved direct local -> container SSH by key
- the request depends on local committed, staged, unstaged, or untracked **non-ignored** files
- the user expects “run my current local workspace remotely” instead of “run the latest pushed branch remotely”

## Do not use this skill when

- the main task is adding or repairing a machine, SSH, or container bootstrap
- the task is generic fork / remote topology setup
- the task is ordinary local coding with no remote execution
- the runtime cannot yet accept direct key-based login
- the user only wants a Git commit, push, or PR

## Critical rules

- Treat the **local working tree** as the source of truth: committed + staged + unstaged + untracked non-ignored.
- Do **not** require the user to commit or push before parity.
- Do **not** use `scp`, `sftp`, `rsync`, `sshpass`, or `expect`.
- Do **not** require GitHub credentials on the host or in the container.
- Keep the sync path **container-only** after machine attach: no host storage root, no host mirror, no host lock.
- For parallel agent work, use `parity_sync.py --session-id <id>`. Session mode derives the workspace root, container endpoint, workspace id, and container identity from `.vaws-local/sessions/<id>/session.json`.
- Use synthetic snapshot refs so dirty working trees can move through Git transport.
- Keep container cache / lock / manifest paths isolated by `workspace_id` under a container-local cache root.
- Preserve runtime-private paths under `/vllm-workspace`, in particular `Mooncake/` (image-provided runtime) and `.vaws-runtime/` (workspace-managed runtime artifacts such as profiler dumps consumed by downstream skills). The exact list lives in `DEFAULT_ROOT_PRESERVE_PATHS` in `scripts/remote_code_parity.py`.
- Container locks should record owner metadata and recover stale lock directories after the bounded stale interval; failed mirror hydration should best-effort clean any matching legacy `git-receive-pack` process trees and discard that repo's partial mirror before retry.
- Keep `stdout` reserved for one final JSON summary and stream phase progress on `stderr` as `__VAWS_PARITY_PROGRESS__=<json>`.
- Runtime install progress should be attributable at the package-step level: uninstall, `vllm`, `vllm-ascend` requirements, `vllm-ascend`, import smoke, and marker write.
- Publish each synthetic snapshot to both the parity ref and an advertised branch ref inside the container-local mirror. Use Git bundles imported inside the container so parentless transport snapshots do not depend on remote receive-pack negotiation or remote base objects.
- Materialize child repos explicitly; do not rely on `git submodule update` to fetch synthetic child commits.
- Synthetic commits are deterministic parentless tree snapshots. Keep each repo's real `HEAD` separately as `source_head` for reinstall drift detection instead of using it as the transport parent.
- If a clean child repo only differs from the parent through the parentless transport commit id, suppress that transport-only child gitlink path from the parent repo's `changed_paths`.
- Use dynamic Python / pip discovery plus a shell-safe env preamble, and source optional Ascend env scripts under a `set +u` / `set -u` guard instead of relying on shell-specific variables.
- Runtime dependency installs use the single A3-tested HuaweiCloud pip index. Do not configure default extra indexes, mirror fallback, or caller-provided pip index overrides in parity.
- Runtime editable installs use `--no-deps`; dependency ownership stays in `vllm-ascend` requirements. Cache / compile env such as `PIP_CACHE_DIR`, `FETCHCONTENT_BASE_DIR`, `VAWS_BUILD_JOBS`, `MAX_JOBS`, and `CMAKE_BUILD_PARALLEL_LEVEL` may be explicitly passed into the remote install shell, and the effective remote install env is recorded in the manifest/runtime state with URL userinfo redacted.
- If editable install or dependency verification fails, fail closed with the captured log instead of trying alternate package sources, installing `uv`, refreshing the packaging stack, or changing install modes.
- Before invoking parity, confirm the local working tree represents the **intended deployment state**. If any submodule source files have uncommitted changes made for temporary debugging or hypothesis testing, revert them before syncing — do not sync exploratory patches to the remote.
- If a previous parity sync in this session led to a failed remote execution and the agent subsequently modified local code, do not re-sync until the root cause of the failure is confirmed from remote logs (not from hypothesis).
- Fail closed if parity cannot be proven.
- First replacement of image-provided `vllm` / `vllm-ascend` requires explicit user consent for that logical container identity.
- `install_consent.py set`, `batch-set`, and `set-sync-mode` must include `--approved-by-user`.
- If the user explicitly says to use local `vllm` / `vllm-ascend`, replace image packages, or run current workspace code remotely, record both decisions in one atomic write: `set-sync-mode --sync-mode local --allow-first-install --approved-by-user`. Do not ask a second first-install question for the same container identity.
- Keep local runtime state only under `.vaws-local/remote-code-parity/`.

## Preconditions

This skill assumes an upper skill already proved:

- container SSH works by key
- the runtime root path is known
- recursive submodules are initialized and populated
- the target machine / container is the intended execution target

If any of those are uncertain, stop and route back to `machine-management` or `repo-init`.

## Local state

Keep local untracked state here:

- `.vaws-local/remote-code-parity/install-consents.json`
- `.vaws-local/remote-code-parity/runtime-state.json`
- `install-consents.json` and `runtime-state.json` writes must be atomic and lock-protected.

Container-local cache layout under the cache root:

- `workspaces/<workspace_id>/mirrors/`
- `workspaces/<workspace_id>/locks/`
- `workspaces/<workspace_id>/manifests/`

## Cross-platform launcher rule

- macOS / Linux / WSL: `python3 ...`
- Windows: `py -3 ...`

Container commands in this skill assume Linux shells.

## Script-first entry points

Normal agent entrypoint:

- POSIX: `python3 .agents/skills/remote-code-parity/scripts/parity_sync.py (--machine <alias-or-ip> | --session-id <id>) ...`
- Windows: `py -3 .agents/skills/remote-code-parity/scripts/parity_sync.py --machine <alias-or-ip> ...`

Apply-mode split:

- `--apply-mode source-only`: publish source snapshots to the container cache only; no runtime materialization and no install/rebuild.
- `--apply-mode materialize`: publish snapshots and update the runtime source tree; no install/rebuild.
- `--apply-mode install`: default full parity behavior with consent, materialization, install/rebuild triggers, and verification.

Agent-facing sync tools:

- `python3 .agents/scripts/remote_sync_plan.py ... --mode source-only|materialize|install`
- `python3 .agents/scripts/remote_sync_apply.py ... --mode source-only|materialize|install`

Consent helper:

- POSIX: `python3 .agents/skills/remote-code-parity/scripts/install_consent.py resolve ...`
- POSIX: `python3 .agents/skills/remote-code-parity/scripts/install_consent.py set ... --approved-by-user`
- POSIX: `python3 .agents/skills/remote-code-parity/scripts/install_consent.py set-sync-mode ... --sync-mode local --allow-first-install --approved-by-user`
- POSIX: `python3 .agents/skills/remote-code-parity/scripts/install_consent.py batch-set --input FILE.json --approved-by-user`

Low-level helper:

- POSIX: `python3 .agents/skills/remote-code-parity/scripts/remote_code_parity.py sync ...`

Optional cache cleanup helper:

- POSIX: `python3 .agents/skills/remote-code-parity/scripts/gc_runtime_cache.py ...`

Reference files:

- `.agents/skills/remote-code-parity/references/behavior.md`
- `.agents/skills/remote-code-parity/references/command-recipes.md`
- `.agents/skills/remote-code-parity/references/acceptance.md`

## Workflow

### 1. Check sync mode before anything else

Before running parity for a container, check the persisted `sync_mode`:

- `unset` (first use): the agent must proactively ask the user whether to sync local code (`local`) or use the container's image-provided vllm + vllm-ascend (`image`). If the user chooses local replacement, record it via `install_consent.py set-sync-mode --sync-mode local --allow-first-install --approved-by-user`; if the user chooses image packages, record `--sync-mode image --approved-by-user`.
- `local`: proceed with the full parity flow below.
- `image`: `parity_sync.py` returns `status: skipped` immediately. The agent skips parity and proceeds with remote execution using image-provided packages.

The user can switch sync mode at any time. `--force-reinstall` overrides `image` mode.
If the agent forgets to set sync mode, `parity_sync.py` returns `status: blocked` before remote mutation.

### 2. Resolve the ready target from inventory

For normal agent work, start from `parity_sync.py`. Use `--session-id` when the task was created by `session-management`; use legacy `--machine` only for single-tenant base-container work.

Collect from local machine inventory:

- machine alias
- container SSH endpoint
- runtime root inside the container
- logical container identity: `<container-name>@<runtime-root>`
- workspace id

Stop if the request is not actually about imminent remote execution.

### 3. Capture synthetic snapshot refs

Create synthetic Git commits for the workspace repo and nested submodules in **postorder**:

1. leaf submodules first
2. then parent submodules
3. workspace root last

For each repo:

- build a temporary index from `HEAD`
- stage the full current working tree with `git add -A`
- reset local-only denylist paths and child-submodule paths from that temporary index
- replace child submodule gitlinks with the child synthetic snapshot commit ids
- write a deterministic parentless synthetic commit for the resulting tree
- record the repo's original `HEAD` as `source_head`; do not make the synthetic commit a child of that `HEAD`, because an empty container mirror would otherwise receive full vLLM history on first push
- filter transport-only child gitlink paths out of `changed_paths` when the child `source_head` matches the parent gitlink and the child has no logical changes

Ignored files stay ignored. The snapshot source of truth is tracked + untracked non-ignored.

### 4. Publish mirrors directly into the container cache

For each repo in scope:

- ensure the container-local bare mirror repo exists under the cache root
- create a local Git bundle for the synthetic ref, stream it to the container over SSH, and fetch that bundle into the container-local bare mirror
- update `refs/parity/<workspace_id>/current` and an advertised branch ref inside that same mirror
- write a compact manifest for this sync attempt under `manifests/`
- use a **container-local** lock while mutating cache or runtime state

Preferred scope:

- workspace root
- `vllm/`
- `vllm-ascend/`
- recursive nested populated submodules if discovered

### 5. Handle first-time runtime replacement

Use a container-side marker under `/vllm-workspace/.remote-code-parity/` to detect whether editable replacement already happened.

If the container identity has never been approved:

- resolve the consent state from `.vaws-local/remote-code-parity/install-consents.json`
- if there is no `allow`, stop with `status == blocked`
- do **not** silently continue with the image-provided packages
- if the user already approved `local` sync with `--allow-first-install`, this consent is already present; do not ask again

If the user already approved this container identity:

- uninstall image-provided `vllm` / `vllm-ascend` best-effort
- delete `/vllm-workspace/vllm` and `/vllm-workspace/vllm-ascend`
- do **not** delete the entire `/vllm-workspace`

### 6. Materialize the mirrors in place inside `/vllm-workspace`

Inside the container:

- initialize the root repo in place if needed
- fetch each repo from the container-local mirror path
- force the runtime repo to the synthetic parity ref
- rewrite submodule URLs to container-local mirror paths
- rewrite submodule URLs to those mirror paths and recursively materialize child repos explicitly
- preserve runtime-private paths such as `Mooncake`, `.vaws-runtime`, and `.remote-code-parity`
- ensure the checked-out commits match the manifest

Do not claim success before the container-side commit ids match the snapshot manifest.

### 7. Reinstall only when required after first install

After the first approved replacement, reinstall only when one of the following triggers fires:

**Trigger 1 — changed-path pattern match:**

- `vllm`: `requirements*`, `pyproject.toml`, `setup.*`, `CMake*`, `cmake/**`, `csrc/**`, and common native-source suffixes
- `vllm-ascend`: same as `vllm`, plus `vllm_ascend/_cann_ops_custom/**`
- pure Python, docs, configs, tests, and ordinary scripts: parity only, no rebuild

**Trigger 2 — commit drift from last sync:**

Compare each repo's real `source_head` commit with `last_head_commits` recorded in `runtime-state.json`. Synthetic snapshot commits are parentless transport commits, so drift detection must use the underlying source HEAD to catch submodule version switches without treating ordinary dirty Python edits as rebuild triggers. If a repo's HEAD changed (e.g. the user did `git checkout v0.8.0` inside `vllm/`), trigger reinstall for that repo even when `changed_paths` is empty.

**Trigger 3 — dependency cascade:**

When `vllm` triggers reinstall (by either trigger), `vllm-ascend` is also reinstalled because it depends on `vllm` internals.

**Uninstall scope:**

Only uninstall the packages that will actually be reinstalled. If only `vllm-ascend` needs reinstall, `vllm` is not uninstalled. On first install, the uninstall step is skipped because `first_install_prepare_script` already handled it.

**Force reinstall:**

Pass `--force-reinstall` to `parity_sync.py` to unconditionally reinstall both `vllm` and `vllm-ascend` regardless of what changed. This overrides all trigger logic above but still runs the full sync flow (snapshot, mirror hydration, materialize, install, verify).

**`--force-reinstall` usage discipline:**

Use `--force-reinstall` only when (a) it is the first sync to a new container, (b) the previous install is known to be broken, or (c) the user explicitly requests it. Do not default to `--force-reinstall` as a precaution — the trigger matrix above already handles normal cases, and unnecessary force-reinstall adds 5–15 minutes of remote compilation time per invocation.

**No-change fast path:**

If all snapshot commits match `last_snapshot_commits` and no reinstall is needed (and `--force-reinstall` is not set), the sync verifies container-side commits with a single SSH call and returns `status == ready` immediately, skipping mirror hydration, materialize, and manifest upload.

Use these commands inside the container when required. The normal path first unifies the runtime Python across `python`, `python3`, CMake, and CANN helper tools, sources optional Ascend env scripts under a `set +u` / `set -u` guard, then uses pip with the single A3-tested HuaweiCloud index. Editable installs use `--no-deps`; dependency ownership stays in the `vllm-ascend` requirements step so `vllm` cannot upgrade `numpy` away from the CANN-compatible version. Build parallelism defaults to `min(available CPUs, 128)` through both `MAX_JOBS` and `CMAKE_BUILD_PARALLEL_LEVEL`. Do not bootstrap `uv`, probe mirror candidates, retry across indexes, or retry by dropping `--no-build-isolation`.

### `vllm`

```bash
export VLLM_TARGET_DEVICE=empty
export TORCH_DEVICE_BACKEND_AUTOLOAD=0
pip install --no-deps -e . --no-build-isolation
```

### `vllm-ascend`

```bash
pip install -r requirements.txt
export MAX_JOBS="${MAX_JOBS:-128}"
export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-$MAX_JOBS}"
pip install --no-deps -v -e . --no-build-isolation
```

### 8. Finish with proof, not assumptions

- Finish with real import smoke (`import vllm`, `import vllm_ascend`, `import torch_npu`) instead of `find_spec()` only, and keep the generated smoke snippet syntactically valid under shell heredoc quoting.


Return a compact JSON summary that includes:

- final `status`
- `container_cache_root`
- synthetic snapshot commit ids
- observed runtime commit ids
- whether reinstall ran or was blocked
- whether this was the first install path
- the reason when the skill stopped early

Success means `status == ready` and runtime commit ids match the synthetic snapshot ids exactly.
