# Remote-code-parity behavior reference

This file defines the durable behavior of `remote-code-parity`.

## Core contract

- Treat the local working tree as the source of truth.
- Use Git transport without requiring a real user commit.
- Prefer host-local bare mirror repos over push-to-GitHub / pull-from-GitHub.
- Keep all local parity state under `.vaws-local/remote-code-parity/`.
- Fail closed when parity cannot be proven.
- Prove the final container-side commit ids instead of trusting command exit status alone.

## Scope and routing

`remote-code-parity` is an internal execution skill. End users should not need to learn `sync`, `session`, or `fleet` vocabulary.

Intended route:

1. `machine-management` proves host + container reachability and key-based login.
2. `remote-code-parity` proves code and runtime package parity.
3. a higher-level serving / benchmark / smoke workflow executes the requested workload.

## Preconditions

Before this skill mutates anything remotely, confirm all of these:

- the target host already accepts key-based SSH
- the target container already accepts direct local -> container key-based SSH
- the runtime root inside the container is known
- the workspace submodules are initialized and populated
- the target machine / container is already the intended execution target

If a required submodule path exists but is not a populated Git worktree, fail closed with a clear instruction to initialize submodules first.

## Why synthetic snapshots exist

A plain `git push HEAD` is not enough because it misses:

- staged-but-uncommitted changes
- unstaged changes
- untracked files
- ignored-but-allowed local runtime inputs

The load-bearing trick is to create synthetic commits from the local working tree without forcing the user to make a real commit. Git remains the transport layer, but the exported commit represents the live working tree rather than the checked-in branch tip.

## Default repo scope

Default scope is:

- workspace root
- `vllm/`
- `vllm-ascend/`
- recursively discovered nested populated submodules

Why:

- the workspace root carries gitlinks
- `vllm` and `vllm-ascend` are both execution inputs
- nested submodules can otherwise drift to commits the parent snapshot cannot materialize

If recursive discovery finds an unpopulated child, fail closed. Do not silently ignore it.

## Recommended denylist

Ignored files are **not** the denylist.

Recommended denylist for parity snapshots:

- `.vaws-local/`
- `.workspace.local/`
- `.machine-inventory.json`
- `.codex/`
- `.claude/settings.local.json`
- `.env`
- `.env.*`
- `.venv/`
- `venv/`
- `__pycache__/`
- `.pytest_cache/`
- `.mypy_cache/`
- `.ruff_cache/`
- `*.log`
- `*.out`
- `.DS_Store`
- `._*`
- `Thumbs.db`

Everything else is allowed by default unless the repo later adds a stronger local-state boundary.

## Local-state contract

Store local parity state under `.vaws-local/remote-code-parity/`.

### `install-consents.json`

Suggested shape:

```json
{
  "schema_version": 1,
  "consents": {
    "server-a": {
      "containers": {
        "container-id-123": {
          "decision": "allow",
          "updated_at": "2026-04-05T10:00:00Z",
          "note": "approved by user"
        }
      }
    }
  }
}
```

### `runtime-state.json`

Suggested shape:

```json
{
  "schema_version": 1,
  "servers": {
    "server-a": {
      "storage_root": "/mnt/nvme/vaws",
      "containers": {
        "container-id-123": {
          "runtime_root": "/vllm-workspace",
          "last_sync_at": "2026-04-05T10:03:00Z",
          "first_reinstall_completed": true,
          "last_snapshot_commits": {
            ".": "abc123",
            "vllm": "def456",
            "vllm-ascend": "ghi789"
          }
        }
      }
    }
  }
}
```

The state is local and advisory. Proof of current parity still comes from the actual container-side commit ids.

## Remote cache layout

Recommended layout under `storage_root`:

```text
<storage_root>/
  remote-code-parity/
    workspaces/<workspace_id>/
      locks/
      manifests/
      logs/
      mirrors/
        workspace.git
        nested/<sanitized-relpath>.git
```

Keep the mirror cache on the **host local disk**, not on a shared filesystem.

## Storage-root policy

A chosen `storage_root` should satisfy all of these:

- writable from the host
- bind-mounted or otherwise visible inside the container
- filesystem type not in the rejected set
- enough free space for mirrors + manifests + rebuilds

Suggested rejected filesystem markers:

- `nfs`
- `nfs4`
- `cifs`
- `smbfs`
- `sshfs`
- `fuse.sshfs`
- `lustre`
- `ceph`
- `glusterfs`

Thresholds:

- hard fail below 512 MiB
- fail closed for first-time editable reinstall below 4 GiB

## First-time runtime replacement

The first sync against a fresh container has a special rule.

The container image may already include its own `vllm` / `vllm-ascend`. If the user wants remote execution to use the local workspace code, the image-provided packages must be replaced with editable installs from the mirrored runtime tree.

That step is mutating and potentially slow, so:

- require user consent once per container identity
- support batch approval through the consent helper
- fail closed if the user declines or has not approved yet

After a container is rebuilt or recreated, treat it as a new identity and require consent again.

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

Everything else defaults to “parity only, no reinstall”.

## Exact proof to collect

A trustworthy parity result records:

- storage root actually used
- pushed synthetic commit ids
- fetched / checked-out commit ids in the container
- whether `vllm` and `vllm-ascend` were reinstalled
- whether first-time consent was consulted
- any mismatch between the manifest and runtime state

## Failure handling

### Fail closed immediately

- no usable `storage_root`
- uninitialized or unpopulated required submodule
- host mirror push failed
- mirror path not visible inside the container
- expected commit id not present in the runtime repo
- reinstall required but blocked or failed

### Recoverable but reportable

- no reinstall needed
- GC skipped
- optional manifest upload skipped even though parity succeeded

## Optional transport fallback

The preferred path is host-local bare mirrors.

If mirror publication is impossible but SSH stdin/stdout still works, a later implementation can stream `git bundle` files over plain `ssh` without using `scp`. That fallback should remain explicit and secondary so the normal path stays incremental and cache-friendly.
