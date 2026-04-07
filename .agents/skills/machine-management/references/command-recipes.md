Prefer the task wrappers for normal add / verify / repair / remove work. Wrappers emit live phase events on `stderr` as `__VAWS_PROGRESS__=<json>` and keep the final structured result on `stdout`.

# Machine-management command recipes

Prefer the task-oriented wrappers. Treat the low-level helpers as fallback maintenance tools.

## Public workflow wrappers

macOS / Linux / WSL:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py --host 173.125.1.2 --image rc
python3 .agents/skills/machine-management/scripts/machine_verify.py --machine 173.125.1.2
python3 .agents/skills/machine-management/scripts/machine_repair.py --machine 173.125.1.2
python3 .agents/skills/machine-management/scripts/machine_remove.py --machine 173.125.1.2
```

Windows:

```powershell
py -3 .agents/skills/machine-management/scripts/machine_add.py --host 173.125.1.2 --image rc
py -3 .agents/skills/machine-management/scripts/machine_verify.py --machine 173.125.1.2
py -3 .agents/skills/machine-management/scripts/machine_repair.py --machine 173.125.1.2
py -3 .agents/skills/machine-management/scripts/machine_remove.py --machine 173.125.1.2
```

## Add one new machine

If the local machine profile already exists and host key SSH is already healthy, the minimum form is:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2 \
  --image rc
```

The wrapper will detect A2 / A3 / 310P from `npu-smi` when possible and append `-a3` or `-310p` automatically for selector-based images.

If `npu-smi` cannot identify the hardware cleanly, pass an explicit override:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2 \
  --image rc \
  --machine-type A3
```

If the profile is missing and the user chose a specific username:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2 \
  --image main \
  --machine-username alice123
```

If the user explicitly accepted the default/random option:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2 \
  --image main \
  --generate-machine-username
```

If host key SSH is missing and the password can be hidden in an env var:

```bash
export VAWS_SSH_PASSWORD='YOUR_PASSWORD'
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2 \
  --image main \
  --password-env VAWS_SSH_PASSWORD
unset VAWS_SSH_PASSWORD
```

PowerShell example:

```powershell
$env:VAWS_SSH_PASSWORD = 'YOUR_PASSWORD'
py -3 .agents/skills/machine-management/scripts/machine_add.py `
  --host 173.125.1.2 `
  --image main `
  --password-env VAWS_SSH_PASSWORD
Remove-Item Env:VAWS_SSH_PASSWORD
```

If the user already exposed the password in chat and the tool cannot hide stdin or env:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2 \
  --image main \
  --password 'YOUR_PASSWORD_ALREADY_IN_CHAT'
```

If the user explicitly wants the latest final release track instead of the recommended `rc` track:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2 \
  --image stable
```

If the user explicitly wants the upstream `main` image track:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2 \
  --image main
```

## Verify one managed machine

```bash
python3 .agents/skills/machine-management/scripts/machine_verify.py \
  --machine 173.125.1.2
```

## Repair one managed machine

Use the machine identifier already recorded in inventory.

```bash
python3 .agents/skills/machine-management/scripts/machine_repair.py \
  --machine 173.125.1.2
```

If the recorded image is legacy or the user wants to rotate to a different track:

```bash
python3 .agents/skills/machine-management/scripts/machine_repair.py \
  --machine 173.125.1.2 \
  --image main
```

If the host hardware probe needs an explicit override during repair:

```bash
python3 .agents/skills/machine-management/scripts/machine_repair.py \
  --machine 173.125.1.2 \
  --image rc \
  --machine-type 310P
```

If host key SSH drifted and a password bootstrap is needed again for recovery:

```bash
python3 .agents/skills/machine-management/scripts/machine_repair.py \
  --machine 173.125.1.2 \
  --password 'YOUR_PASSWORD_ALREADY_IN_CHAT'
```

## Remove one managed machine

```bash
python3 .agents/skills/machine-management/scripts/machine_remove.py \
  --machine 173.125.1.2
```

## Local profile and inventory inspection

These are still useful for debugging or reporting local state:

```bash
python3 .agents/scripts/workspace_profile.py summary
python3 .agents/skills/machine-management/scripts/inventory.py summary
```

## Low-level fallback helpers

Use these only when the workflow wrapper cannot express the requested maintenance.

Probe one host:

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py probe-host \
  --host 173.125.1.2 \
  --image main \
  --machine-type A3
```

Bootstrap host key auth directly:

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-host-key \
  --host 173.125.1.2 \
  --password 'YOUR_PASSWORD_ALREADY_IN_CHAT'
```

Bootstrap or repair one managed container directly:

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-container \
  --host 173.125.1.2 \
  --name vaws-alice123 \
  --port 46671 \
  --namespace alice123 \
  --image main \
  --machine-type A3 \
  --soc ascend910_9391
```

Run the smoke test directly:

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py smoke \
  --host 173.125.1.2 \
  --port 46671
```

Manual inventory write with hardware metadata:

```bash
python3 .agents/skills/machine-management/scripts/inventory.py upsert \
  --alias 173.125.1.2 \
  --machine-username alice123 \
  --host 173.125.1.2 \
  --name vaws-alice123 \
  --container-port 46671 \
  --image quay.nju.edu.cn/ascend/vllm-ascend:main-a3 \
  --machine-type A3 \
  --soc ascend910_9391 \
  --container-type A3
```

Notes:

- `--bootstrap-method` is optional. New records default to `ssh`; updates preserve the existing stored value.
- compatibility aliases still work in the low-level helpers, but the wrappers intentionally document only the narrow canonical surface.
