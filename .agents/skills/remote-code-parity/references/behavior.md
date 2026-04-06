# Remote-code-parity behavior reference

This file defines the durable behavior of `remote-code-parity`.

## Core contract

- Treat the local working tree as the source of truth.
- Use Git transport without requiring a real user commit.
- Keep sync container-only after machine attach.
- Keep all local parity state under `.vaws-local/remote-code-parity/`.
- Fail closed when parity cannot be proven.
- Prove the final container-side commit ids instead of trusting command exit status alone.

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
- use a container-local lock while mutating cache or runtime state
- do not create or reuse a shared flat host path such as `/home/vaws`

## Runtime materialization model

Runtime root defaults to `/vllm-workspace`.

Materialization requirements:

- initialize the root repo in place when `.git` is missing
- force the root repo and submodules to the synthetic snapshot commits
- rewrite submodule URLs to container-local mirror paths
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

Recommended conservative defaults:

### `vllm`

Trigger reinstall on changes matching:

- `requirements*`
- `pyproject.toml`
- `setup.py`
- `setup.cfg`
- `CMakeLists.txt`
- `cmake/**`
- `csrc/**`
- common native-source suffixes such as `*.cu`, `*.cuh`, `*.cpp`, `*.cc`, `*.h`, `*.hpp`

### `vllm-ascend`

Trigger reinstall on the same set as `vllm`, plus:

- `vllm_ascend/_cann_ops_custom/**`

Everything else defaults to parity-only, no reinstall.

## Exact proof to collect

A trustworthy parity result records:

- container cache root actually used
- pushed synthetic commit ids
- checked-out commit ids in the container
- whether `vllm` and `vllm-ascend` were reinstalled
- whether first-time consent was consulted or blocked
- any mismatch between the manifest and runtime state
