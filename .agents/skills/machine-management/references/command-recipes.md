# Machine-management command recipes

These are fallback patterns. Prefer the helper scripts in `scripts/` when possible.

## Inventory

```bash
python3 .agents/skills/machine-management/scripts/inventory.py summary
python3 .agents/skills/machine-management/scripts/inventory.py get 173.125.1.2
```

## Probe one host

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py probe-host \
  --host 173.125.1.2
```

## Bootstrap or repair one managed container

```bash
python3 .agents/skills/machine-management/scripts/manage_machine.py bootstrap-container \
  --host 173.125.1.2 \
  --container-name vaws-maoxx241 \
  --container-ssh-port 46671
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
  --host-ip 173.125.1.2 \
  --container-name vaws-maoxx241 \
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
  --container-name vaws-maoxx241 \
  --container-ssh-port 46671 \
  --clean-local-known-hosts

python3 .agents/skills/machine-management/scripts/inventory.py remove 173.125.1.2
```

## Only fallback raw step: first interactive host bootstrap

Use a single interactive SSH session only when host key auth is not ready yet and the request is adding a new machine.

```bash
ssh -p 22 root@173.125.1.2
mkdir -p /root/.ssh && chmod 700 /root/.ssh
cat >> /root/.ssh/authorized_keys <<'KEY'
<LOCAL_PUBLIC_KEY_LINE>
KEY
chmod 600 /root/.ssh/authorized_keys
```
