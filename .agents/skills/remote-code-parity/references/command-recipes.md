# Remote-code-parity command recipes

Prefer the helper scripts in `scripts/` when possible.

## Inspect the current consent state

```bash
python3 .agents/skills/remote-code-parity/scripts/install_consent.py resolve   --repo-root .   --server-name blue-a   --container-identity c-20260405-01
```

## Approve the first runtime replacement for one container

```bash
python3 .agents/skills/remote-code-parity/scripts/install_consent.py set   --repo-root .   --server-name blue-a   --container-identity c-20260405-01   --decision allow   --note "approved for first editable install"
```

## Bulk-approve several containers at once

Input file example:

```json
[
  {
    "server_name": "blue-a",
    "container_identity": "c-20260405-01",
    "decision": "allow"
  },
  {
    "server_name": "blue-b",
    "container_identity": "c-20260405-09",
    "decision": "deny",
    "note": "leave image packages intact"
  }
]
```

Apply:

```bash
python3 .agents/skills/remote-code-parity/scripts/install_consent.py batch-set   --repo-root .   --input approvals.json
```

## Dry-run the local parity plan

```bash
python3 .agents/skills/remote-code-parity/scripts/remote_code_parity.py plan   --workspace-root .   --workspace-id vaws-main   --server-name blue-a   --container-identity c-20260405-01   --runtime-root /vllm-workspace   --storage-root /mnt/nvme/vaws
```

## Full sync against an already-known storage root

```bash
python3 .agents/skills/remote-code-parity/scripts/remote_code_parity.py sync   --workspace-root .   --workspace-id vaws-main   --server-name blue-a   --host lab.example.internal   --host-port 22   --host-user root   --container-host lab.example.internal   --container-port 41001   --container-user root   --container-identity c-20260405-01   --runtime-root /vllm-workspace   --storage-root /mnt/nvme/vaws
```

## Probe storage-root candidates on the first run

```bash
python3 .agents/skills/remote-code-parity/scripts/remote_code_parity.py sync   --workspace-root .   --workspace-id vaws-main   --server-name blue-a   --host lab.example.internal   --host-port 22   --host-user root   --container-host lab.example.internal   --container-port 41001   --container-user root   --container-identity c-20260405-01   --runtime-root /vllm-workspace   --storage-root-candidate /mnt/nvme/vaws   --storage-root-candidate /mnt/data/vaws   --storage-root-candidate /data/vaws
```

## Force a dry-run without touching the host or container

```bash
python3 .agents/skills/remote-code-parity/scripts/remote_code_parity.py sync   --workspace-root .   --workspace-id vaws-main   --server-name blue-a   --host lab.example.internal   --host-port 22   --host-user root   --container-host lab.example.internal   --container-port 41001   --container-user root   --container-identity c-20260405-01   --runtime-root /vllm-workspace   --storage-root /mnt/nvme/vaws   --dry-run
```

## Clean old parity artifacts

```bash
python3 .agents/skills/remote-code-parity/scripts/gc_runtime_cache.py   --storage-root /mnt/nvme/vaws   --workspace-id vaws-main   --keep-success 3   --keep-failure 1   --dry-run
```

## Recommended upper-skill routing rule

When a serving / benchmark / smoke workflow is about to execute remotely:

1. ensure `machine-management` already proved host + container SSH
2. call `remote-code-parity`
3. continue only if `status == ready`

Do **not** continue on `blocked` or `failed`.
