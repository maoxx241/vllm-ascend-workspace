# Remote Toolbox Behavior

The toolbox normalizes a VAWS target before any remote action. Resolution
accepts `--machine`, `--session-id`, or `--session-file` and returns the host
SSH endpoint, container SSH endpoint, runtime root, workspace id, session id,
leased devices, and state paths.

`remote_probe` verifies observed runtime facts from the running container. It
reports the recorded image tag separately from host `docker inspect` image id
because tags are not trusted as proof of the active runtime.

`remote_exec` stores complete stdout/stderr locally and returns tails in JSON.
Long tasks should use `remote_job_start` so agents can call status, tail, stop,
and collect without manual process management.

`remote_sync_plan` is a planning surface and must not mutate remote state.
`remote_sync_apply --mode source-only` publishes source snapshots to the
container cache only. `--mode materialize` checks sources out into the runtime
root without running pip/build/install steps. `--mode install` keeps the full
remote-code-parity install behavior.

Artifact push/pull always verifies SHA-256 hashes from a manifest and streams
over SSH shell commands. It must not rely on scp, sftp, or rsync.
