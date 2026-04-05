# Machine-management command recipes

Prefer the task-oriented wrappers. Treat the low-level helpers as fallback maintenance tools.

## Public workflow wrappers

macOS / Linux / WSL:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py --host 173.125.1.2
python3 .agents/skills/machine-management/scripts/machine_verify.py --machine 173.125.1.2
python3 .agents/skills/machine-management/scripts/machine_repair.py --machine 173.125.1.2
python3 .agents/skills/machine-management/scripts/machine_remove.py --machine 173.125.1.2
```

Windows:

```powershell
py -3 .agents/skills/machine-management/scripts/machine_add.py --host 173.125.1.2
py -3 .agents/skills/machine-management/scripts/machine_verify.py --machine 173.125.1.2
py -3 .agents/skills/machine-management/scripts/machine_repair.py --machine 173.125.1.2
py -3 .agents/skills/machine-management/scripts/machine_remove.py --machine 173.125.1.2
```

## Add one new machine

If the local machine profile already exists and host key SSH is already healthy, the minimum form is:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2
```

If the profile is missing and the user chose a specific username:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2 \
  --machine-username alice123
```

If the user explicitly accepted the default/random option:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2 \
  --generate-machine-username
```

If host key SSH is missing and the password can be hidden in an env var:

```bash
export VAWS_SSH_PASSWORD='YOUR_PASSWORD'
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2 \
  --password-env VAWS_SSH_PASSWORD
unset VAWS_SSH_PASSWORD
```

PowerShell example:

```powershell
$env:VAWS_SSH_PASSWORD = 'YOUR_PASSWORD'
py -3 .agents/skills/machine-management/scripts/machine_add.py `
  --host 173.125.1.2 `
  --password-env VAWS_SSH_PASSWORD
Remove-Item Env:VAWS_SSH_PASSWORD
```

If the user already exposed the password in chat and the tool cannot hide stdin or env:

```bash
python3 .agents/skills/machine-management/scripts/machine_add.py \
  --host 173.125.1.2 \
  --password 'YOUR_PASSWORD_ALREADY_IN_CHAT'
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
  --host 173.125.1.2
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
  --namespace alice123
```

Run the smoke test directly:

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py smoke \
  --host 173.125.1.2 \
  --port 46671
```

Manual inventory write:

```bash
python3 .agents/skills/machine-management/scripts/inventory.py upsert \
  --alias 173.125.1.2 \
  --machine-username alice123 \
  --host 173.125.1.2 \
  --name vaws-alice123 \
  --container-port 46671
```

Notes:

- `--bootstrap-method` is optional. New records default to `ssh`; updates preserve the existing stored value.
- compatibility aliases still work in the low-level helpers, but the wrappers intentionally document only the narrow canonical surface.
