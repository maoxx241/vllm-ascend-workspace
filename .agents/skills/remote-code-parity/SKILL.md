---
name: remote-code-parity
description: Inspect or publish local workspace sources onto a prepared container work root before direct remote smoke. Do not use for managed coordinator executions, machine bootstrap, or generic Git topology work.
---

# Remote Code Parity

Coordinator `vaws_run` prepares managed sources. Do not call
`parity_sync.py --execution-id` against a live execution root.

Direct source-only inspection may use this skill against an explicit
`--host` and optional `--runtime-root` (default `/vllm-workspace`). The
workspace wrapper does not install, materialize, or record first-install
consent. Those belong to coordinator preparation.

## Use this skill when

- a direct container SSH endpoint already works
- the request is to inspect or publish local sources into a prepared root
- the user is not asking to mutate a live coordinator execution

## Do not use this skill when

- a coordinator execution already owns the work root
- the task is machine attach, serving, or generic remote I/O
- the user only wants a Git commit, push, or PR

## Entry points

```bash
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py \
  --host <container-ip> --print-derived-args
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py \
  --host <container-ip> [--runtime-root /vllm-workspace] [--dry-run]
python3 .agents/scripts/remote_sync_plan.py --host <container-ip>
python3 .agents/scripts/remote_sync_apply.py --host <container-ip>
```

Low-level package CLI (coordinator-owned install/materialize, not the
normal agent path):

```bash
python3 .agents/skills/remote-code-parity/scripts/remote_code_parity.py sync ...
```

`stdout` is one Result Envelope v1. Progress is `__VAWS_PROGRESS__=` on
`stderr`.

Deleted: `install_consent.py`, `--session-id`, `--machine`, first-install
gates as a required normal flow.
