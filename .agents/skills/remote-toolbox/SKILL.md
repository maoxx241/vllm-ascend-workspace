---
name: remote-toolbox
description: Resolve, probe, execute, observe jobs, sync, manage service lifecycle, transfer artifacts, and clean VAWS remote Ascend session containers through structured agent-facing CLI entrypoints.
---

# VAWS Remote Toolbox

Use this skill when an agent needs to operate a managed VAWS machine or session
container with local-tool-like semantics instead of writing raw SSH, scp, sftp,
manual tail, or manual ps/kill commands.

## Critical Rules

- Prefer `--session-id` or `--session-file` for parallel work. Use `--machine`
  only for explicitly single-tenant legacy flows.
- Entry points stream progress to `stderr` as `__VAWS_REMOTE_TOOLBOX_PROGRESS__=<json>`.
- Final `stdout` is exactly one JSON object.
- JSON failures must use one of: `needs_input`, `blocked`, `failed`,
  `timeout`, `needs_repair`, `cancelled`.
- Artifact transfer uses SSH streaming plus manifest/hash verification. Do not
  depend on scp, sftp, or rsync.
- `remote_sync_plan --mode source-only` and
  `remote_sync_apply --mode source-only` must not enter install/rebuild.
- `remote_sync_apply --mode materialize` updates runtime sources but still skips
  install/rebuild.
- `remote_sync_apply --mode install` delegates to remote-code-parity full
  install/rebuild behavior and consent gates.

## Entry Points

Target and diagnostics:

```bash
python3 .agents/scripts/remote_target_resolve.py (--machine <alias> | --session-id <id> | --session-file <path>)
python3 .agents/scripts/remote_probe.py (--machine <alias> | --session-id <id> | --session-file <path>)
python3 .agents/scripts/remote_exec.py --session-id <id> --cwd /vllm-workspace --command 'python3 -V'
# remote_exec and remote_job_start source /etc/profile.d/vaws-ascend-env.sh by default.
# Use --no-runtime-env only when intentionally debugging the raw container shell.
```

Long jobs:

```bash
python3 .agents/scripts/remote_job_start.py --session-id <id> --kind build --command 'bash build.sh'
python3 .agents/scripts/remote_job_status.py --job-id <job-id>
python3 .agents/scripts/remote_job_tail.py --job-id <job-id> --lines 120
python3 .agents/scripts/remote_job_stop.py --job-id <job-id> --force
python3 .agents/scripts/remote_job_collect.py --job-id <job-id>
```

Sync:

```bash
python3 .agents/scripts/remote_sync_plan.py --session-id <id> --mode source-only
python3 .agents/scripts/remote_sync_apply.py --session-id <id> --mode source-only
python3 .agents/scripts/remote_sync_plan.py --session-id <id> --mode install --force-reinstall
python3 .agents/scripts/remote_sync_apply.py --session-id <id> --mode install
```

Service:

```bash
python3 .agents/scripts/remote_service_start.py --session-id <id> -- --model /data/models/Qwen --tp 1
python3 .agents/scripts/remote_service_status.py --session-id <id>
python3 .agents/scripts/remote_service_logs.py --session-id <id> --lines 200
python3 .agents/scripts/remote_service_stop.py --session-id <id> --force
```

Artifacts and cleanup:

```bash
python3 .agents/scripts/remote_artifact_manifest.py --session-id <id> --remote-path /vllm-workspace/.vaws-runtime/serving
python3 .agents/scripts/remote_artifact_pull.py --session-id <id> --remote-path /vllm-workspace/.vaws-runtime/serving --local-dir .vaws-local/remote-toolbox/artifacts/serving
python3 .agents/scripts/remote_artifact_push.py --session-id <id> --local-path ./local-report --remote-path /tmp/report
python3 .agents/scripts/remote_cleanup.py --session-id <id> --service --jobs --remote-temp --leases --dry-run
python3 .agents/scripts/remote_cleanup.py --session-id <id> --job-id <job-id> --dry-run
```

## State

Local untracked state lives under `.vaws-local/remote-toolbox/`:

- `logs/` for full stdout/stderr from `remote_exec`
- `jobs/<job-id>.json` for local job registry records
- `artifacts/` for pulled job/service/profiling artifacts

Remote toolbox state lives under `<runtime-root>/.vaws-runtime/remote-toolbox/`
inside the target container.

## References

- `.agents/skills/remote-toolbox/references/behavior.md`
- `.agents/skills/remote-toolbox/references/command-recipes.md`
- `.agents/skills/remote-toolbox/references/acceptance.md`
- `.agents/skills/remote-toolbox/references/stress-validation.md`
