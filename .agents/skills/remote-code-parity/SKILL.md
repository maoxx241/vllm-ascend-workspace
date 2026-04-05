---
name: remote-code-parity
description: Ensure a ready remote runtime runs the exact current local workspace state before any remote smoke, service launch, or benchmark. Use automatically immediately before remote execution when SCP is unavailable and local uncommitted changes must be reflected remotely. Do not use for initial machine attach, generic Git topology work, or unrelated local-only coding.
---

# Remote Code Parity

Keep a **ready** remote runtime in exact code parity with the local `vllm-ascend-workspace` checkout.

## Use this skill when

- a remote smoke, service launch, or benchmark is about to start
- `machine-management` already proved host SSH and direct local -> container SSH by key
- the request depends on local uncommitted changes, untracked files, or ignored-but-allowed files
- `scp` is unavailable or disallowed
- the user expects “run the latest local code remotely” rather than “run the latest pushed commit remotely”

## Do not use this skill when

- the main task is adding or repairing a machine, SSH, or container bootstrap
- the task is generic fork / remote topology setup
- the task is ordinary local coding with no remote execution
- the runtime cannot yet accept direct key-based login
- the user only wants a Git commit, push, or PR

## Critical rules

- Treat the **local working tree** as the source of truth: committed + staged + unstaged + untracked.
- Do **not** require the user to commit or push before parity.
- Do **not** use `scp`, `sftp`, `rsync`, `sshpass`, or `expect`.
- Do **not** require GitHub credentials on the remote host or in the container.
- Prefer **host-local bare mirror repos** over push-to-GitHub / pull-from-GitHub.
- Use synthetic snapshot refs so dirty working trees can be mirrored through Git transport.
- Keep remote cache / lock / manifest / log paths isolated by `workspace_id`.
- Reuse the first validated `storage_root` for a given host unless it becomes invalid.
- Reject shared or undersized storage roots.
- Fail closed if parity cannot be proven.
- First replacement of image-provided `vllm` / `vllm-ascend` inside a container requires explicit user consent for that **container identity**.
- Once the user approved reinstall for a given container identity, later parity runs on the same container do not ask again.
- Keep local runtime state only under `.vaws-local/remote-code-parity/`.

## Preconditions

This skill assumes an upper skill already proved:

- host SSH works by key
- container SSH works by key
- the runtime root path is known
- recursive submodules are initialized and populated
- the target machine / container is the intended execution target

If any of those are still uncertain, stop and route back to `machine-management` or `repo-init`.

## Local state

Keep local untracked state here:

- `.vaws-local/remote-code-parity/install-consents.json`
- `.vaws-local/remote-code-parity/runtime-state.json`

Recommended remote host cache layout under `storage_root`:

- `remote-code-parity/workspaces/<workspace_id>/mirrors/`
- `remote-code-parity/workspaces/<workspace_id>/locks/`
- `remote-code-parity/workspaces/<workspace_id>/manifests/`
- `remote-code-parity/workspaces/<workspace_id>/logs/`

## Cross-platform launcher rule

- macOS / Linux / WSL: `python3 ...`
- Windows: `py -3 ...`

Remote host and runtime commands in this skill assume Linux shells.

## Script-first entry points

Primary orchestration:

- POSIX: `python3 .agents/skills/remote-code-parity/scripts/remote_code_parity.py plan ...`
- POSIX: `python3 .agents/skills/remote-code-parity/scripts/remote_code_parity.py sync ...`
- Windows: `py -3 .agents/skills/remote-code-parity/scripts/remote_code_parity.py plan ...`
- Windows: `py -3 .agents/skills/remote-code-parity/scripts/remote_code_parity.py sync ...`

Consent helper:

- POSIX: `python3 .agents/skills/remote-code-parity/scripts/install_consent.py resolve ...`
- POSIX: `python3 .agents/skills/remote-code-parity/scripts/install_consent.py set ...`
- POSIX: `python3 .agents/skills/remote-code-parity/scripts/install_consent.py batch-set --input FILE.json`

Cache cleanup helper:

- POSIX: `python3 .agents/skills/remote-code-parity/scripts/gc_runtime_cache.py --storage-root ... --workspace-id ...`

Reference files:

- `.agents/skills/remote-code-parity/references/behavior.md`
- `.agents/skills/remote-code-parity/references/command-recipes.md`
- `.agents/skills/remote-code-parity/references/acceptance.md`

## Workflow

### 1. Confirm scope and preconditions

Collect:

- `workspace_root`
- `workspace_id`
- host SSH endpoint
- container SSH endpoint
- runtime root inside the container
- container identity
- either a known `storage_root` or a probe candidate list

Stop if the request is not actually about an imminent remote execution.

### 2. Resolve `storage_root`

Rules:

- if local state already records a validated `storage_root` for the host, reuse it
- otherwise probe candidates on the host and select the first path that is:
  - writable
  - not on a rejected shared filesystem type
  - above the hard free-space threshold
- persist the chosen `storage_root` locally for future parity runs

Thresholds:

- hard fail below **512 MiB**
- for a first-time uninstall + editable reinstall, fail below **4 GiB**

### 3. Capture synthetic snapshot refs

Create synthetic Git commits for the workspace repo and nested submodules in **postorder**:

1. leaf submodules first
2. then parent submodules
3. workspace root last

For each repo:

- build a temporary index from `HEAD`
- stage the full current working tree with `git add -A -f`
- exclude local-only denylist paths
- replace child submodule gitlinks with the child synthetic snapshot commit ids
- write a synthetic commit

This is the key step that allows Git transport to represent dirty working trees.

### 4. Publish mirrors to the host

For each repo in scope:

- ensure the host-local bare mirror repo exists under `storage_root`
- push the synthetic commit to a stable parity ref such as `refs/parity/<workspace_id>/current`
- write a compact manifest for this sync attempt under `manifests/`

Preferred scope:

- workspace root
- `vllm/`
- `vllm-ascend/`
- recursive nested populated submodules if discovered

### 5. Materialize the mirrors inside the runtime container

Inside the container:

- fetch each repo from the host-local mirror path
- force the runtime repo to the synthetic parity ref
- rewrite submodule URLs to local mirror paths
- initialize / update submodules against those mirror paths
- ensure the checked-out commits match the manifest

Do not claim success before the container-side commit ids match the snapshot manifest.

### 6. Handle first-time runtime replacement

If this container identity has never been approved:

- resolve the consent state from `.vaws-local/remote-code-parity/install-consents.json`
- if there is no `allow`, stop with `status == blocked`
- do **not** silently continue with the image-provided packages

If the user already approved this container identity:

- best-effort uninstall image-provided `vllm` / `vllm-ascend`
- rebuild/install from the mirrored runtime tree

Use these commands inside the container when required:

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

Default environment preamble before install / import smoke:

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null || true
source /usr/local/Ascend/nnal/atb/set_env.sh 2>/dev/null || true
source /vllm-workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash 2>/dev/null || true
export PATH=/usr/local/python3.11.14/bin:$PATH
export PYTHON=/usr/local/python3.11.14/bin/python3
export PIP=/usr/local/python3.11.14/bin/pip
export VLLM_WORKER_MULTIPROC_METHOD=spawn
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
```

### 7. Reinstall trigger matrix after first install

After the first approved replacement, reinstall only when matching paths changed.

Conservative defaults:

- `vllm`: `requirements*`, `pyproject.toml`, `setup.*`, `CMake*`, `cmake/**`, `csrc/**`, and common native-source suffixes
- `vllm-ascend`: same as `vllm`, plus `vllm_ascend/_cann_ops_custom/**`
- pure Python, docs, configs, tests, and ordinary scripts: parity only, no rebuild

### 8. Finish with proof, not assumptions

Return a compact JSON summary that includes:

- final `status`
- `storage_root` used
- synthetic snapshot commit ids
- observed runtime commit ids
- whether reinstall ran or was blocked
- the reason when the skill stopped early

Success means `status == ready` and runtime commit ids match the synthetic snapshot ids exactly.
