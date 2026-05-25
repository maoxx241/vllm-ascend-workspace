# Behavior Reference

## Session Creation

`session_create.py` resolves a session id, allocates local leases, creates or reuses a Git worktree, writes a session spec under `.vaws-local/sessions/<session-id>/session.json`, then bootstraps a dedicated remote container through the existing machine-management bootstrap logic.

The base machine inventory is treated as a resource pool. Creating a session does not replace or mutate the base machine record.

## Worktree Behavior

Default worktree path:

```text
../vaws-worktrees/<repo-name>/<session-id>
```

Default branch:

```text
session/<session-id>
```

If a worktree already exists and is bound to the same session, it is reused. If it is bound to a different session or has no binding, creation fails closed.

## Container Behavior

Session containers use the base machine image, host mounts, workdir, and Ascend bootstrap logic, but get a distinct container name and SSH port.

The default container name is:

```text
vaws-<namespace>-<session-id>
```

Session bootstrap defaults to a host-local prepared image cache. The bootstrapper first prefers a local exact base-image hit over a registry pull for non-moving image policies, then derives `vaws-session-prepared:<base-image-id>-ssh-v2` from that image. On a cache miss, the new session container is created from the base image, installs `openssh-server` / `openssh-client`, configures pip / pytest basics, and commits the prepared image before dynamic SSH configuration. On a cache hit, the session container starts from the prepared image and skips the repeated package-manager and pip / pytest bootstrap work.

The prepared image cache is session-specific behavior; managed base-machine add / repair paths keep their conservative raw-image bootstrap unless they explicitly opt in. `VAWS_DOCKER_PULL_POLICY=always` forces a fresh pull check, and `session_create.py --disable-prepared-image-cache` disables prepared-image usage for raw bootstrap timing or debugging.

After bootstrap, session creation defaults to SSH-only verification. It checks host SSH and direct container SSH, records `npu_smoke_skipped: true`, and marks the session ready when both endpoints are reachable. This avoids serializing every parallel agent behind repeated `torch` / `torch_npu` smoke checks and avoids consuming an NPU during session setup. Use `session_create.py --verification-mode full` when the creation step itself must prove the NPU runtime with the full smoke check.

Explicit `--session-id --no-worktree` sessions are treated as shared-root timing/debug sessions. They write the session record and leases, but do not overwrite the repo-root `.vaws-local/current-session.json`; downstream commands should receive `--session-id` or `--session-file` explicitly.

When no explicit/env session id is provided, `session_create.py` generates a fresh id instead of reusing `.vaws-local/current-session.json`. Current-session lookup remains available for commands that operate on an existing session.

## Lease Behavior

Leases are local to the workspace and live in `.vaws-local/sessions/leases.json`.

The first implementation protects:

- container SSH ports
- service ports
- explicitly requested or auto-counted NPU devices

Session-aware serving uses the session NPU lease as its default device set. If a launch requests explicit devices, they must be contained inside the session's leased device list.

Session creation probes host listening ports once before taking the lease lock, then selects a container SSH port from that snapshot. This avoids holding the global lease file lock across one SSH round trip per candidate port.

NPU leases are released by `session_remove.py --release-leases`, not by `serve_stop.py`, because a session may stop serving and continue with another remote task.

When `session_remove.py --remove-container` sees no session serving state file, it skips the serving stop wrapper and relies on container removal to terminate any untracked process. This keeps teardown cheap for sessions that were created only for parity, bootstrap timing, or compile work.

`session_remove.py` marks a session `removed` only when the requested container/worktree removal succeeds. Failed removal leaves the session in `needs_repair`. `session_gc.py` releases leases for `removed` or missing-state sessions; it does not release leases for generic `failed` sessions because those may still protect partially created remote resources.

## Legacy Compatibility

Legacy `--machine` flows continue to use the base machine container and machine-level state. Session-aware flows use `--session-id` or `--session-file` and state under `.vaws-local/sessions/<session-id>/`.
