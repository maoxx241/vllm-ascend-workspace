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
- Use synthetic snapshot refs so dirty working trees can move through Git transport.
- Keep container cache / lock / manifest paths isolated by `workspace_id` under a container-local cache root.
- Preserve runtime-private paths under `/vllm-workspace`, especially `Mooncake`.
- Keep `stdout` reserved for one final JSON summary and stream phase progress on `stderr` as `__VAWS_PARITY_PROGRESS__=<json>`.
- Publish each synthetic snapshot to both the parity ref and an advertised branch ref inside the container-local mirror.
- Materialize child repos explicitly; do not rely on `git submodule update` to fetch synthetic child commits.
- Use dynamic Python / pip discovery and the Ascend driver `LD_LIBRARY_PATH` preamble instead of pinning one Python patch path.
- If editable install fails because the image packaging stack is too old, attempt one bounded packaging-stack refresh before failing closed.
- Fail closed if parity cannot be proven.
- First replacement of image-provided `vllm` / `vllm-ascend` requires explicit user consent for that logical container identity.
- `install_consent.py set` and `batch-set` must include `--approved-by-user`.
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

- POSIX: `python3 .agents/skills/remote-code-parity/scripts/parity_sync.py --machine <alias-or-ip> ...`
- Windows: `py -3 .agents/skills/remote-code-parity/scripts/parity_sync.py --machine <alias-or-ip> ...`

Consent helper:

- POSIX: `python3 .agents/skills/remote-code-parity/scripts/install_consent.py resolve ...`
- POSIX: `python3 .agents/skills/remote-code-parity/scripts/install_consent.py set ... --approved-by-user`
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

### 1. Resolve the ready target from inventory

For normal agent work, start from `parity_sync.py`.

Collect from local machine inventory:

- machine alias
- container SSH endpoint
- runtime root inside the container
- logical container identity: `<container-name>@<runtime-root>`
- workspace id

Stop if the request is not actually about imminent remote execution.

### 2. Capture synthetic snapshot refs

Create synthetic Git commits for the workspace repo and nested submodules in **postorder**:

1. leaf submodules first
2. then parent submodules
3. workspace root last

For each repo:

- build a temporary index from `HEAD`
- stage the full current working tree with `git add -A`
- reset local-only denylist paths and child-submodule paths from that temporary index
- replace child submodule gitlinks with the child synthetic snapshot commit ids
- write a synthetic commit

Ignored files stay ignored. The snapshot source of truth is tracked + untracked non-ignored.

### 3. Publish mirrors directly into the container cache

For each repo in scope:

- ensure the container-local bare mirror repo exists under the cache root
- push the synthetic commit to `refs/parity/<workspace_id>/current` and to an advertised branch ref inside that same mirror
- write a compact manifest for this sync attempt under `manifests/`
- use a **container-local** lock while mutating cache or runtime state

Preferred scope:

- workspace root
- `vllm/`
- `vllm-ascend/`
- recursive nested populated submodules if discovered

### 4. Handle first-time runtime replacement

Use a container-side marker under `/vllm-workspace/.remote-code-parity/` to detect whether editable replacement already happened.

If the container identity has never been approved:

- resolve the consent state from `.vaws-local/remote-code-parity/install-consents.json`
- if there is no `allow`, stop with `status == blocked`
- do **not** silently continue with the image-provided packages

If the user already approved this container identity:

- uninstall image-provided `vllm` / `vllm-ascend` best-effort
- delete `/vllm-workspace/vllm` and `/vllm-workspace/vllm-ascend`
- do **not** delete the entire `/vllm-workspace`

### 5. Materialize the mirrors in place inside `/vllm-workspace`

Inside the container:

- initialize the root repo in place if needed
- fetch each repo from the container-local mirror path
- force the runtime repo to the synthetic parity ref
- rewrite submodule URLs to container-local mirror paths
- rewrite submodule URLs to those mirror paths and recursively materialize child repos explicitly
- preserve runtime-private paths such as `Mooncake` and `.remote-code-parity`
- ensure the checked-out commits match the manifest

Do not claim success before the container-side commit ids match the snapshot manifest.

### 6. Reinstall only when required after first install

After the first approved replacement, reinstall only when matching paths changed.

Conservative defaults:

- `vllm`: `requirements*`, `pyproject.toml`, `setup.*`, `CMake*`, `cmake/**`, `csrc/**`, and common native-source suffixes
- `vllm-ascend`: same as `vllm`, plus `vllm_ascend/_cann_ops_custom/**`
- pure Python, docs, configs, tests, and ordinary scripts: parity only, no rebuild

Use these commands inside the container when required. The normal path first tries the in-place environment, then does one bounded packaging refresh / retry when legacy packaging metadata blocks editable install:

### `vllm`

```bash
export VLLM_TARGET_DEVICE=empty
pip install -e . --no-build-isolation
```

### `vllm-ascend`

```bash
pip install -r requirements.txt
pip install -v -e . --no-build-isolation
```

### 7. Finish with proof, not assumptions

Return a compact JSON summary that includes:

- final `status`
- `container_cache_root`
- synthetic snapshot commit ids
- observed runtime commit ids
- whether reinstall ran or was blocked
- whether this was the first install path
- the reason when the skill stopped early

Success means `status == ready` and runtime commit ids match the synthetic snapshot ids exactly.
