# Remote-code-parity behavior reference

This file defines the durable behavior of `remote-code-parity`.

## Core contract

- Treat the local working tree as the source of truth.
- Use Git transport without requiring a real user commit.
- Keep sync container-only after machine attach.
- Keep all local parity state under `.vaws-local/remote-code-parity/`.
- Fail closed when parity cannot be proven.
- Prove the final container-side commit ids instead of trusting command exit status alone.
- Stream phase progress on `stderr` as `__VAWS_PARITY_PROGRESS__=<json>` and keep one final JSON payload on `stdout`.
- Keep runtime-install phases attributable instead of collapsing them into one opaque step: uninstall, `vllm`, `vllm-ascend` requirements, `vllm-ascend`, import verification, and marker write should each surface their own progress event.

## Sync mode gate

Before invoking parity for a container, the agent checks the persisted `sync_mode` in `install-consents.json`:

- `unset`: first use — agent must ask the user whether to sync local code (`local`) or use image-provided packages (`image`), then record via `install_consent.py set-sync-mode`.
- `local`: proceed with the full parity flow.
- `image`: `parity_sync.py` returns `status: skipped` immediately; the agent proceeds with remote execution using image-provided packages without syncing or installing.

`--force-reinstall` overrides `image` mode. The user can switch sync mode at any time.

## Scope and routing

`remote-code-parity` is an internal execution skill.

Intended route:

1. `machine-management` proves direct local -> container SSH and records the target in inventory.
2. `remote-code-parity` proves code and runtime package parity.
3. a higher-level serving / benchmark / smoke workflow executes the requested workload.

Normal agent-facing entrypoint: `parity_sync.py`.

## Preconditions

Before this skill mutates anything remotely, confirm all of these:

- the target container already accepts direct local -> container key-based SSH
- the runtime root inside the container is known
- the workspace submodules required for execution are initialized and populated
- the selected machine record in inventory is the intended runtime target

## Source of truth and snapshot semantics

The snapshot source of truth is:

- tracked files
- staged files
- unstaged tracked changes
- untracked non-ignored files

The snapshot source of truth is **not**:

- ignored local caches
- `.vaws-local/`
- temporary agent state
- other denylisted local-only files

Implementation rule:

- stage with `git add -A`
- then reset denylisted paths and child-submodule paths from the temporary index
- do **not** use `git add -A -f` for the main snapshot path

## Cache and transport model

The sync path does not rely on host storage.

Container-local cache root defaults to:

- `/root/.cache/vaws/remote-code-parity`

Per-workspace layout:

```text
<cache-root>/
  workspaces/
    <workspace_id>/
      mirrors/
      locks/
      manifests/
```

Required behavior:

- create bare mirror repos inside the container cache root
- push synthetic refs directly from local -> container SSH
- also publish an advertised branch ref inside each mirror so ordinary fetch paths can see the latest synthetic snapshot
- use a container-local lock while mutating cache or runtime state
- do not create or reuse a shared flat host path such as `/home/vaws`

## Runtime materialization model

Runtime root defaults to `/vllm-workspace`.

Materialization requirements:

- initialize the root repo in place when `.git` is missing
- force the root repo and submodules to the synthetic snapshot commits
- rewrite submodule URLs to container-local mirror paths
- materialize child repos explicitly instead of relying on `git submodule update` to fetch synthetic child commits
- when a child repo snapshot has the same tree as its original `HEAD`, reuse that original commit so parent repos do not see a gitlink-only synthetic delta
- preserve runtime-private sibling paths such as `Mooncake`
- preserve `.remote-code-parity` so the container-side marker survives root cleanups
- do not delete the entire runtime root as part of normal sync

## First-time runtime replacement

The first sync against a fresh container has a special rule.

The container image may already include its own `vllm` / `vllm-ascend`. If the user wants remote execution to use the local workspace code, those image-provided packages must be replaced with editable installs from the synced source trees.

That step is mutating and potentially slow, so:

- require user consent once per logical container identity
- require `--approved-by-user` before writing consent state
- fail closed if the user declines or has not approved yet
- use a container-side marker under `/vllm-workspace/.remote-code-parity/runtime-install.json` to detect whether first install already happened

First-install mutation boundary:

- uninstall image packages best-effort
- delete `/vllm-workspace/vllm`
- delete `/vllm-workspace/vllm-ascend`
- keep the rest of `/vllm-workspace`, including `Mooncake`

## Reinstall trigger matrix

Reinstall fires when **any** of these conditions is true:

### Trigger 1 — changed-path pattern match

#### `vllm`

Trigger reinstall on changes matching:

- `requirements*`
- `pyproject.toml`
- `setup.py`
- `setup.cfg`
- `CMakeLists.txt`
- `cmake/**`
- `csrc/**`
- common native-source suffixes such as `*.cu`, `*.cuh`, `*.cpp`, `*.cc`, `*.h`, `*.hpp`

#### `vllm-ascend`

Trigger reinstall on the same set as `vllm`, plus:

- `vllm_ascend/_cann_ops_custom/**`

Everything else defaults to parity-only, no reinstall.

### Trigger 2 — commit drift from last sync

Compare each repo's real HEAD commit with `last_head_commits` in `runtime-state.json`. Synthetic snapshot commits change whenever the working tree is dirty, so drift detection must use the underlying HEAD to avoid false positives from pure-Python edits. If the HEAD differs (e.g. submodule version switch via `git checkout`), trigger reinstall for that repo even when `changed_paths` is empty because the tree matches the new HEAD.

### Trigger 3 — dependency cascade

When `vllm` triggers reinstall (by either trigger above), `vllm-ascend` is also reinstalled because it depends on `vllm` internals.

### Uninstall scope

Only uninstall the packages that will actually be reinstalled:

- `reinstall_vllm` only → uninstall `vllm`
- `reinstall_vllm_ascend` only → uninstall `vllm-ascend` / `vllm_ascend`
- both → uninstall all three

On first install the reinstall-branch uninstall step is skipped because `first_install_prepare_script` already removed image-provided packages.

### Force reinstall

`--force-reinstall` unconditionally sets `reinstall_vllm` and `reinstall_vllm_ascend` to true, overriding all trigger logic. The full sync flow still runs (snapshot, push, materialize, install, verify). Useful for recovering from a broken editable install or validating the install pipeline without touching source files.

### No-change fast path

When all snapshot commits match `last_snapshot_commits` and no reinstall trigger fires (and `--force-reinstall` is not set), the sync verifies the container-side commits with a single SSH call and returns `status == ready` immediately, skipping push, materialize, and manifest upload.

## Exact proof to collect

A trustworthy parity result records:

- container cache root actually used
- pushed synthetic commit ids
- checked-out commit ids in the container
- whether `vllm` and `vllm-ascend` were reinstalled
- whether first-time consent was consulted or blocked
- any mismatch between the manifest and runtime state

## Runtime install compatibility

- discover the runtime Python dynamically from `/usr/local/python*/bin/python3`, then fall back to `python3` or `python`
- unify that interpreter across `python`, `python3`, `HI_PYTHON`, `Python_EXECUTABLE`, `Python3_EXECUTABLE`, and CMake-driven helper processes before editable install
- preseed the runtime `PATH` and the Ascend driver `LD_LIBRARY_PATH` prefix, then source optional env scripts under a `set +u` / `set -u` guard so shell-specific variables are not required
- keep the fast path on `pip install -e . --no-build-isolation`
- route pip installs through mirror-aware fallback in the order Tsinghua -> Aliyun -> PyPI
- if editable install fails because the image packaging stack is too old for the current `pyproject.toml`, attempt one bounded packaging-stack refresh and one retry before failing closed
- finish runtime verification with real imports, not `find_spec()` alone, and keep the generated heredoc smoke snippet valid Python after shell quoting
- after import smoke test, verify all `vllm-ascend` declared dependencies are version-satisfied (`verify-deps`); if a mismatch is detected (e.g. `numpy<2.0.0` but `numpy 2.x` installed), automatically reinstall `vllm-ascend` requirements and re-verify before failing
- surface a progress transition before each long runtime-install package step so an agent can tell whether the wait is in uninstall, requirements, editable install, or verification
- keep consent and runtime-state writes atomic so parallel wrapper calls do not clobber local state
