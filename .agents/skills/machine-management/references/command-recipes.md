# Machine-management command recipes

These are preferred command patterns for the skill.

## Inventory

Inspect local state:

```bash
python3 .agents/skills/machine-management/scripts/inventory.py summary
python3 .agents/skills/machine-management/scripts/inventory.py get 125.173.1.2
```

Write or update a record after successful add or identity-changing repair:

```bash
python3 .agents/skills/machine-management/scripts/inventory.py put \
  --alias 125.173.1.2 \
  --host-ip 125.173.1.2 \
  --host-port 22 \
  --host-user root \
  --container-name vaws-maoxx241 \
  --container-ssh-port 46037 \
  --image quay.nju.edu.cn/ascend/vllm-ascend:latest \
  --bootstrap-method password-once \
  --last-verified-at "2026-04-04T12:34:56Z"
```

Remove a record after successful cleanup:

```bash
python3 .agents/skills/machine-management/scripts/inventory.py remove 125.173.1.2
```

## Local public key discovery

Prefer an existing public key. Typical choices:

```bash
ls -1 ~/.ssh/*.pub
```

If there is more than one, prefer the user’s existing default key rather than creating a new workspace-specific key.

## Host SSH probe by key

Probe host SSH by key before any mutation:

```bash
ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p 22 root@125.173.1.2 'echo host-ssh-ok'
```

## First and only password bootstrap

Use a one-off interactive SSH session only when key auth is not ready yet and the request is adding a new machine.

Once inside the host, append the local public key to root’s `authorized_keys`:

```bash
install -d -m 700 /root/.ssh && \
cat >> /root/.ssh/authorized_keys <<'KEY'
<LOCAL_PUBLIC_KEY_LINE>
KEY
chmod 600 /root/.ssh/authorized_keys
```

Do not automate this with `sshpass` or `expect`.

## Host prerequisite probes

Check Docker:

```bash
ssh -p 22 root@125.173.1.2 'docker --version && docker info >/dev/null'
```

Check required devices and mounts:

```bash
ssh -p 22 root@125.173.1.2 '
set -e
for p in \
  /dev/davinci_manager \
  /dev/hisi_hdc \
  /dev/devmm_svm \
  /usr/local/Ascend/driver \
  /usr/local/dcmi \
  /usr/local/bin/npu-smi \
  /usr/local/sbin \
  /usr/share/zoneinfo/Asia/Shanghai
  do
  test -e "$p"
done
'
```

Inspect large shared mounts when you need extra bind mounts:

```bash
ssh -p 22 root@125.173.1.2 'df -h'
```

## Proxy usefulness probe

Only pass proxy vars into the container when the proxy is actually useful.

Compare behavior without and with the host’s current proxy env:

```bash
ssh -p 22 root@125.173.1.2 '
set -u
probe() {
  label="$1"
  url="$2"
  if curl -I --connect-timeout 5 --max-time 15 "$url" >/dev/null 2>&1; then
    echo "$label=ok"
  else
    echo "$label=fail"
  fi
}
orig_http_proxy="${http_proxy-}"
orig_https_proxy="${https_proxy-}"
orig_HTTP_PROXY="${HTTP_PROXY-}"
orig_HTTPS_PROXY="${HTTPS_PROXY-}"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
probe no_proxy_baidu https://www.baidu.com
probe no_proxy_google https://www.google.com
if [ -n "$orig_http_proxy$orig_https_proxy$orig_HTTP_PROXY$orig_HTTPS_PROXY" ]; then
  export http_proxy="$orig_http_proxy" https_proxy="$orig_https_proxy" HTTP_PROXY="$orig_HTTP_PROXY" HTTPS_PROXY="$orig_HTTPS_PROXY"
  probe with_proxy_baidu https://www.baidu.com
  probe with_proxy_google https://www.google.com
else
  echo with_proxy_baidu=unset
  echo with_proxy_google=unset
fi
'
```

Interpretation:

- `no_proxy_baidu=ok` is expected.
- `no_proxy_google=fail` is acceptable in restricted environments.
- Pass proxy vars into the container only when `with_proxy_google=ok` and the proxy does not break Baidu.

## Choose a high SSH port

Pick an unused high port from `46000-46999`:

```bash
ssh -p 22 root@125.173.1.2 '
python3 - <<"PY"
import random, subprocess
out = subprocess.check_output(["ss", "-ltnH"], text=True)
used = set()
for line in out.splitlines():
    local = line.split()[3]
    if ":" in local:
        try:
            used.add(int(local.rsplit(":", 1)[1]))
        except ValueError:
            pass
for port in random.sample(range(46000, 47000), 1000):
    if port not in used:
        print(port)
        raise SystemExit(0)
raise SystemExit("no free port in 46000-46999")
PY
'
```

## Pull the image from the mirror

Always use the mirror, not an overseas default registry:

```bash
ssh -p 22 root@125.173.1.2 'docker pull quay.nju.edu.cn/ascend/vllm-ascend:latest'
```

## Canonical container start recipe

Required core flags:

```bash
ssh -p 22 root@125.173.1.2 '
docker run --name vaws-maoxx241 -it -d --network host --shm-size=500g \
  --privileged=true \
  -w /vllm-workspace \
  --device=/dev/davinci_manager \
  --device=/dev/hisi_hdc \
  --device=/dev/devmm_svm \
  --entrypoint=bash \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
  -v /usr/local/dcmi:/usr/local/dcmi \
  -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
  -v /usr/local/sbin:/usr/local/sbin \
  -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime:ro \
  -v /home:/home \
  -v /tmp:/tmp \
  -v /weight:/weight \
  -v /data:/data \
  -v /mnt:/mnt \
  quay.nju.edu.cn/ascend/vllm-ascend:latest
'
```

If some optional data paths do not exist, omit only those specific bind mounts.

If proxy pass-through is warranted, add:

```bash
-e http_proxy="$http_proxy" \
-e https_proxy="$https_proxy"
```

## Install and configure container SSH

Install packages without quiet flags and with the known apt tolerances:

```bash
ssh -p 22 root@125.173.1.2 '
docker exec vaws-maoxx241 bash -lc "
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
stdbuf -oL -eL apt-get -o Acquire::Check-Date=false -o Acquire::Check-Valid-Until=false update
stdbuf -oL -eL apt-get install -y openssh-server openssh-client --fix-missing
"
'
```

Configure `sshd` for a high port and key-only root login, then start it:

```bash
ssh -p 22 root@125.173.1.2 '
docker exec vaws-maoxx241 bash -lc "
set -euo pipefail
port=46037
config=/etc/ssh/sshd_config
set_opt() {
  key="$1"
  value="$2"
  if grep -Eq "^#?${key}([[:space:]].*)?$" "$config"; then
    sed -ri "s|^#?${key}([[:space:]].*)?$|${key} ${value}|" "$config"
  else
    printf "%s %s\n" "$key" "$value" >> "$config"
  fi
}
mkdir -p /root/.ssh /run/sshd
set_opt Port "$port"
set_opt PasswordAuthentication no
set_opt PubkeyAuthentication yes
set_opt PermitRootLogin prohibit-password
/usr/sbin/sshd -t
pkill sshd || true
/usr/sbin/sshd
"
'
```

## Add the local key to the container

```bash
ssh -p 22 root@125.173.1.2 '
docker exec vaws-maoxx241 bash -lc "
set -euo pipefail
install -d -m 700 /root/.ssh
cat >> /root/.ssh/authorized_keys <<'\''KEY'\''
<LOCAL_PUBLIC_KEY_LINE>
KEY
chmod 600 /root/.ssh/authorized_keys
"
'
```

## Direct container SSH probe

Once container SSH is configured, prefer direct access:

```bash
ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p 46037 root@125.173.1.2 'echo container-ssh-ok'
```

## Host firewall open recipes

### `ufw`

```bash
ssh -p 22 root@125.173.1.2 'ufw allow 46037/tcp'
```

### `firewalld`

```bash
ssh -p 22 root@125.173.1.2 '
firewall-cmd --permanent --add-port=46037/tcp && firewall-cmd --reload
'
```

### `iptables`

```bash
ssh -p 22 root@125.173.1.2 '
iptables -C INPUT -p tcp --dport 46037 -j ACCEPT || \
iptables -I INPUT -p tcp --dport 46037 -j ACCEPT
'
```

Do not remove firewall rules during server removal in v1.

## Mesh key generation inside the container

Generate a stable mesh key only inside the managed container when needed:

```bash
ssh -p 46037 root@125.173.1.2 '
set -euo pipefail
if [ ! -f /root/.ssh/id_ed25519 ]; then
  ssh-keygen -t ed25519 -C "vaws-mesh:125.173.1.2" -f /root/.ssh/id_ed25519 -N ""
fi
cat /root/.ssh/id_ed25519.pub
'
```

Add a peer mesh key to another managed container:

```bash
ssh -p 46041 root@125.173.1.3 '
set -euo pipefail
install -d -m 700 /root/.ssh
cat >> /root/.ssh/authorized_keys <<'\''KEY'\''
<PEER_MESH_PUBLIC_KEY_LINE>
KEY
chmod 600 /root/.ssh/authorized_keys
'
```

Prime peer `known_hosts` inside a managed container:

```bash
ssh -p 46037 root@125.173.1.2 '
set -euo pipefail
mkdir -p /root/.ssh
ssh-keyscan -p 46041 125.173.1.3 >> /root/.ssh/known_hosts 2>/dev/null
'
```

## Container-side NPU smoke test

Run the smoke test inside the managed container with the pinned Python:

```bash
ssh -p 46037 root@125.173.1.2 '
set -euo pipefail
source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
source /vllm-workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash
export PATH=/usr/local/python3.11.14/bin:$PATH
export PYTHON=/usr/local/python3.11.14/bin/python3
export PIP=/usr/local/python3.11.14/bin/pip
export VLLM_WORKER_MULTIPROC_METHOD=spawn
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
"$PYTHON" - <<"PY"
import torch
import torch_npu  # noqa: F401
x = torch.zeros(1, 2).npu()
print({"shape": tuple(x.shape), "device": str(x.device)})
assert str(x.device).startswith("npu")
PY
'
```

## Remove the managed container

Delete only the recorded managed container:

```bash
ssh -p 22 root@125.173.1.2 'docker rm -f vaws-maoxx241'
```

If the container is already absent, treat that as successful drift cleanup.

## Clean local `known_hosts`

Remove the container endpoint from local `known_hosts`:

```bash
ssh-keygen -R '[125.173.1.2]:46037'
```

## Clean peer mesh trust on removal

Remove the departing mesh key from a peer managed container:

```bash
ssh -p 46041 root@125.173.1.3 '
set -euo pipefail
tmp=$(mktemp)
grep -v "vaws-mesh:125.173.1.2" /root/.ssh/authorized_keys > "$tmp" || true
cat "$tmp" > /root/.ssh/authorized_keys
rm -f "$tmp"
chmod 600 /root/.ssh/authorized_keys
ssh-keygen -R "[125.173.1.2]:46037" -f /root/.ssh/known_hosts || true
'
```

## Forbidden commands for this skill

Do not use:

```bash
scp ...
sftp ...
sshpass ...
expect ...
```
