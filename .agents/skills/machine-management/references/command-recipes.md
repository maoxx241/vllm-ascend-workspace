# Machine-management command recipes

These are fallback patterns. Prefer the helper scripts in `scripts/` and `.agents/scripts/` when possible.

## Local profile and inventory

macOS / Linux / WSL:

```bash
python3 .agents/scripts/workspace_profile.py summary
python3 .agents/skills/machine-management/scripts/inventory.py summary
```

Windows:

```powershell
py -3 .agents/scripts/workspace_profile.py summary
py -3 .agents/skills/machine-management/scripts/inventory.py summary
```

Create or reuse a collision-safe local machine profile after the user chose a specific name:

```bash
python3 .agents/scripts/workspace_profile.py ensure --username alice123
```

Create a default/random profile only after the user explicitly accepted that option:

```bash
python3 .agents/scripts/workspace_profile.py ensure --generate
```

## Probe one host

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py probe-host \
  --host 173.125.1.2
```

## First host bootstrap with a supplied password

Prefer a hidden env var when the tool can set it without echoing the secret.

POSIX example:

```bash
export VAWS_SSH_PASSWORD='YOUR_PASSWORD'
python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-host-key \
  --host 173.125.1.2 \
  --password-env VAWS_SSH_PASSWORD
unset VAWS_SSH_PASSWORD
```

PowerShell example:

```powershell
$env:VAWS_SSH_PASSWORD = 'YOUR_PASSWORD'
py -3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-host-key `
  --host 173.125.1.2 `
  --password-env VAWS_SSH_PASSWORD
Remove-Item Env:VAWS_SSH_PASSWORD
```

When the agent cannot hide env or stdin and the user already supplied the password in the current chat, the literal flag is allowed as a last resort:

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-host-key \
  --host 173.125.1.2 \
  --password 'YOUR_PASSWORD_ALREADY_IN_CHAT'
```

Manual fallback only when needed:

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-host-key \
  --host 173.125.1.2 \
  --print-command
```

## Bootstrap or repair one managed container

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-container \
  --host 173.125.1.2 \
  --container-name vaws-alice123 \
  --container-ssh-port 46671 \
  --namespace alice123
```

## Run the smoke test

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py smoke \
  --host 173.125.1.2 \
  --container-ssh-port 46671
```

## Verify a machine read-only

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py verify-machine \
  --host 173.125.1.2 \
  --container-ssh-port 46671
```

## Write inventory after success

```bash
python3 .agents/skills/machine-management/scripts/inventory.py put \
  --alias 173.125.1.2 \
  --namespace alice123 \
  --host-ip 173.125.1.2 \
  --container-name vaws-alice123 \
  --container-ssh-port 46671 \
  --bootstrap-method ssh \
  --last-verified-at "2026-04-04T09:22:03Z"
```

Compatibility alias:

```bash
python3 .agents/skills/machine-management/scripts/inventory.py put ... --bootstrap-method key
```

The helper normalizes `key` to stored value `ssh`.

## Mesh trust

Export a mesh key:

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py mesh-export-key \
  --host 173.125.1.2 \
  --container-ssh-port 46671 \
  --comment vaws-mesh:173.125.1.2
```

Add a peer:

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py mesh-add-peer \
  --host 173.131.1.2 \
  --container-ssh-port 46768 \
  --peer-public-key 'ssh-ed25519 AAAA... vaws-mesh:173.125.1.2' \
  --peer-host 173.125.1.2 \
  --peer-port 46671
```

Remove a peer:

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py mesh-remove-peer \
  --host 173.131.1.2 \
  --container-ssh-port 46768 \
  --peer-comment vaws-mesh:173.125.1.2 \
  --peer-host 173.125.1.2 \
  --peer-port 46671
```

## Remove a machine

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py remove-container \
  --host 173.125.1.2 \
  --container-name vaws-alice123 \
  --container-ssh-port 46671 \
  --clean-local-known-hosts

python3 .agents/skills/machine-management/scripts/inventory.py remove 173.125.1.2
```
