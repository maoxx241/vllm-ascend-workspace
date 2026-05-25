# VAWS Remote Toolbox Stress Validation Matrix

This matrix is based on recurring vLLM Ascend development tasks in this
workspace: remote code parity before service/benchmark runs, parallel agent
sessions on 125/131, negative service launches, profiling/report artifact
pullback, and cleanup after failed remote work.

## Local Contract Suite

Run before any remote work:

```bash
python3 -m compileall -q .agents
git diff --check -- .agents AGENTS.md README.md README.en.md
for f in .agents/scripts/remote_*.py; do python3 "$f" --help >/dev/null; done
```

Expected:

- all scripts compile
- no whitespace errors
- each CLI exposes `--help`
- final stdout stays a single JSON object for normal execution paths

## Probe And Target Resolution

Use both managed base machines:

```bash
python3 .agents/scripts/remote_probe.py --machine 173.125.1.2 --timeout 90
python3 .agents/scripts/remote_probe.py --machine 173.131.1.2 --timeout 90
```

Validate:

- host endpoint is `:22`, container endpoint is `:46000` for the base machines
- image proof includes observed `docker inspect` image id, not only a tag
- CANN reports `/usr/local/Ascend/cann-9.0.0`
- Python, torch, torch_npu, NPU facts are present
- known_hosts facts identify both host and container keys

## Parallel Session Isolation

Create at least two sessions on 131:

```bash
python3 .agents/skills/session-management/scripts/session_create.py --machine 173.131.1.2 --session-id stress-a --npu-count 1 --no-worktree --verification-mode ssh
python3 .agents/skills/session-management/scripts/session_create.py --machine 173.131.1.2 --session-id stress-b --npu-count 1 --no-worktree --verification-mode ssh
python3 .agents/scripts/remote_target_resolve.py --session-id stress-a
python3 .agents/scripts/remote_target_resolve.py --session-id stress-b
```

Validate:

- distinct container names
- distinct container SSH ports
- distinct NPU leases
- distinct state files under `.vaws-local/sessions/<id>/`
- `session_list.py` lease state matches the session files

## Source Sync Mode Split

Plan both no-install and install paths:

```bash
python3 .agents/scripts/remote_sync_plan.py --session-id stress-a --mode source-only
python3 .agents/scripts/remote_sync_plan.py --session-id stress-a --mode install --force-reinstall
```

Validate:

- source-only reports `will_install=false`
- source-only reports `will_materialize=false`
- install reports consent state and install/rebuild reasons
- source-only apply emits no runtime-install progress:

```bash
python3 .agents/scripts/remote_sync_apply.py --session-id stress-a --mode source-only
```

## Lightweight Remote Toolbox Pressure

Use `remote_toolbox_stress.py` for repeatable non-NPU pressure:

```bash
python3 .agents/scripts/remote_toolbox_stress.py \
  --machine 173.131.1.2 \
  --parallelism 4 \
  --exec-count 8 \
  --job-count 4 \
  --artifact-files 48 \
  --artifact-bytes 512
```

Expected:

- concurrent `remote_exec` cases succeed
- jobs are observable through start/status/tail
- artifact pull uses `tar` transport when available and verifies hashes
- final stdout is one JSON object with `status=ok`

For heavier artifact pressure, increase file count before increasing byte size:

```bash
python3 .agents/scripts/remote_toolbox_stress.py --machine 173.131.1.2 --artifact-files 512 --artifact-bytes 2048 --skip-jobs --skip-exec
```

This stresses the profiling/report artifact shape without running msprof or
benchmark workloads.

## Negative Service Launch Safety

Invalid model paths must fail before stopping existing services:

```bash
python3 .agents/scripts/remote_service_start.py --machine 173.131.1.2 --model /no/such/model --skip-parity
```

Expected:

- status is `needs_input`
- progress reaches `validate`
- progress does not include `stop-existing`

## Artifact Return Path

For a service log or profiling/report directory:

```bash
python3 .agents/scripts/remote_artifact_manifest.py --session-id stress-a --remote-path /vllm-workspace/.vaws-runtime/serving
python3 .agents/scripts/remote_artifact_pull.py --session-id stress-a --remote-path /vllm-workspace/.vaws-runtime/serving --local-dir .vaws-local/remote-toolbox/artifacts/stress-serving
```

Validate:

- manifest includes file count, size, SHA-256, and relpaths
- pull skips already matching files on rerun
- large multi-file directories use one tar stream when possible
- no scp/sftp/rsync dependency

## Cleanup Proof

Clean test sessions and prove local lease state is empty:

```bash
python3 .agents/scripts/remote_cleanup.py --session-id stress-a --service --jobs --remote-temp --session-container --leases --force
python3 .agents/scripts/remote_cleanup.py --session-id stress-b --service --jobs --remote-temp --session-container --leases --force
python3 .agents/skills/session-management/scripts/session_list.py
```

Expected:

- session statuses are `removed`
- `npu_devices`, `container_ssh_ports`, and `service_ports` are empty for the
  test machine
- service status returns `not_found`

## Agent-Unfriendly Regression Checks

Track and fix these as failures:

- a validation error stops an existing service before proving the new request is valid
- a destructive session command falls back to the current-session binding when
  no explicit `--session-id` or `--session-file` was provided
- GC mutates lease state unless the caller explicitly requested `--apply`
- status/list/help commands perform business logic when the caller requested
  `--help`
- `remote_exec` or `remote_job_start` runs in the raw shell Python by default
  instead of the validated Ascend runtime profile
- cleanup can only delete the entire remote job tree and cannot target one
  `job_id`
- long commands hide progress until completion
- a command requires manual SSH, scp, sftp, tail, ps, or kill for the normal path
- artifact pull opens one SSH connection per file for large report trees
- dry-run output does not clearly state whether install/rebuild would happen
- cleanup cannot prove leases and service state after it runs

Fix expectations:

- `session_remove.py` without an explicit target returns `needs_input`.
- `session_gc.py` defaults to dry-run and only mutates with `--apply`.
- `remote_exec.py` and `remote_job_start.py` source
  `/etc/profile.d/vaws-ascend-env.sh` by default and report the effective
  Python in their environment summary.
- `remote_cleanup.py --job-id <job-id>` scopes remote job cleanup.
