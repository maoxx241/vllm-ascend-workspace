# Machine-management command recipes

These are fallback patterns. Prefer the helper scripts in `scripts/` and `.agents/scripts/` when possible.

## Local profile and inventory

```bash
python3 .agents/scripts/workspace_profile.py summary
python3 .agents/skills/machine-management/scripts/inventory.py summary
```

Create or reuse a collision-safe local machine profile:

```bash
python3 .agents/scripts/workspace_profile.py ensure --username alice123
# or
python3 .agents/scripts/workspace_profile.py ensure
```

## Probe one host

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py probe-host \
  --host 173.125.1.2
```

## First interactive host bootstrap

Preferred helper:

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-host-key \
  --host 173.125.1.2
```

If the execution environment cannot surface an interactive password prompt, print the exact command instead:

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-host-key \
  --host 173.125.1.2 \
  --print-command
```

Raw fallback only when the helper is unavailable:

```bash
ssh -p 22 root@173.125.1.2
mkdir -p /root/.ssh && chmod 700 /root/.ssh
cat >> /root/.ssh/authorized_keys <<'KEY'
<LOCAL_PUBLIC_KEY_LINE>
KEY
chmod 600 /root/.ssh/authorized_keys
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
