#!/usr/bin/env python3
"""Deterministic helpers for machine-management.

These helpers keep remote host/container workflows compact and predictable so
an agent can rely on scripts instead of constructing long SSH heredocs in the
conversation. All subcommands print JSON.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
from dataclasses import dataclass
from typing import Any, Sequence


DEFAULT_IMAGE = "quay.nju.edu.cn/ascend/vllm-ascend:latest"
DEFAULT_WORKDIR = "/vllm-workspace"
DEFAULT_HOST_USER = "root"
DEFAULT_HOST_PORT = 22
DEFAULT_PORT_RANGE = "46000:46999"
DEFAULT_KNOWN_HOSTS = pathlib.Path.home() / ".ssh" / "known_hosts"
SENTINEL = "__VAWS_JSON__="


class MachineManagementError(RuntimeError):
    """Raised for deterministic, user-facing failures."""


@dataclass(frozen=True)
class SshTarget:
    host: str
    user: str = DEFAULT_HOST_USER
    port: int = DEFAULT_HOST_PORT


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def run_local(
    cmd: Sequence[str],
    *,
    input_text: str | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(cmd),
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "command failed"
        raise MachineManagementError(detail)
    return proc


def ssh_command(target: SshTarget, *, batch_mode: bool = True) -> list[str]:
    return [
        "ssh",
        "-o",
        f"BatchMode={'yes' if batch_mode else 'no'}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(target.port),
        f"{target.user}@{target.host}",
    ]


@dataclass
class RemoteResult:
    target: SshTarget
    returncode: int
    stdout: str
    stderr: str
    payload: dict[str, Any] | None


def parse_sentinel(stdout: str) -> dict[str, Any] | None:
    payload: dict[str, Any] | None = None
    for line in stdout.splitlines():
        if line.startswith(SENTINEL):
            try:
                payload = json.loads(line[len(SENTINEL) :])
            except json.JSONDecodeError as exc:
                payload = {"success": False, "error": f"invalid remote JSON: {exc}"}
    return payload


def run_remote_script(
    target: SshTarget,
    script: str,
    *,
    args: Sequence[str] = (),
    batch_mode: bool = True,
) -> RemoteResult:
    cmd = ssh_command(target, batch_mode=batch_mode) + ["bash", "-s", "--", *args]
    proc = run_local(cmd, input_text=script)
    return RemoteResult(
        target=target,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        payload=parse_sentinel(proc.stdout),
    )


def assert_remote_success(result: RemoteResult, *, require_payload: bool = True) -> dict[str, Any]:
    if require_payload and result.payload is None:
        detail = result.stderr.strip() or result.stdout.strip() or "remote command produced no JSON payload"
        raise MachineManagementError(detail)
    if result.returncode != 0:
        if result.payload and result.payload.get("error"):
            raise MachineManagementError(str(result.payload["error"]))
        detail = result.stderr.strip() or result.stdout.strip() or "remote command failed"
        raise MachineManagementError(detail)
    return result.payload or {}


def find_public_key(explicit: str | None = None) -> pathlib.Path:
    if explicit:
        path = pathlib.Path(explicit).expanduser().resolve()
        if not path.exists():
            raise MachineManagementError(f"public key file not found: {path}")
        return path

    candidates = [
        pathlib.Path.home() / ".ssh" / "id_ed25519.pub",
        pathlib.Path.home() / ".ssh" / "id_rsa.pub",
        pathlib.Path.home() / ".ssh" / "id_ecdsa.pub",
    ]
    for path in candidates:
        if path.exists():
            return path.resolve()
    raise MachineManagementError(
        "no local public key found; pass --public-key-file or create ~/.ssh/id_ed25519.pub"
    )


def load_public_key(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text.startswith("ssh-"):
        raise MachineManagementError(f"not a valid SSH public key: {path}")
    return text


def render_host_probe_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
image="$1"
port_range="$2"
prefix="$3"
py=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    py="$cand"
    break
  fi
done
if [ -z "$py" ]; then
  echo "__SENTINEL__{\"success\": false, \"error\": \"python not found on remote host\"}"
  exit 3
fi
"$py" - "$image" "$port_range" "$prefix" <<'PY'
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys

image, port_range, prefix = sys.argv[1:4]
start_s, end_s = port_range.split(":", 1)
start, end = int(start_s), int(end_s)


def run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


result: dict[str, object] = {
    "success": True,
    "hostname": socket.gethostname(),
    "python": shutil.which("python3") or shutil.which("python"),
    "docker": {
        "present": shutil.which("docker") is not None,
        "version": None,
        "info_ok": False,
    },
    "required_paths": {},
    "optional_mounts": [],
    "npu_smi_present": os.path.exists("/usr/local/bin/npu-smi"),
    "image": {
        "name": image,
        "present_local": False,
    },
    "managed_containers": [],
    "free_port": None,
    "free_port_range": [start, end],
    "firewall": {
        "ufw": shutil.which("ufw") is not None,
        "firewalld": shutil.which("firewall-cmd") is not None,
    },
}

required = [
    "/dev/davinci_manager",
    "/dev/hisi_hdc",
    "/dev/devmm_svm",
    "/usr/local/Ascend/driver",
    "/usr/local/dcmi",
    "/usr/local/bin/npu-smi",
    "/usr/local/sbin",
    "/usr/share/zoneinfo/Asia/Shanghai",
]
for item in required:
    result["required_paths"][item] = pathlib.Path(item).exists()

for item in ["/home", "/tmp", "/weight", "/data", "/mnt"]:
    if pathlib.Path(item).exists():
        result["optional_mounts"].append(item)

if result["docker"]["present"]:
    rc, out, _ = run(["docker", "--version"])
    if rc == 0:
        result["docker"]["version"] = out
    rc, _, _ = run(["docker", "info"])
    result["docker"]["info_ok"] = rc == 0
    rc, _, _ = run(["docker", "image", "inspect", image])
    result["image"]["present_local"] = rc == 0
    rc, out, _ = run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"])
    if rc == 0:
        rows = []
        for line in out.splitlines():
            if not line:
                continue
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            name, status, actual_image = parts
            if name.startswith(prefix):
                rows.append({"name": name, "status": status, "image": actual_image})
        result["managed_containers"] = rows

rc, out, _ = run(["ss", "-ltnH"])
used: set[int] = set()
if rc == 0:
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        if ":" not in local:
            continue
        try:
            used.add(int(local.rsplit(":", 1)[1]))
        except ValueError:
            continue
for port in range(start, end + 1):
    if port not in used:
        result["free_port"] = port
        break

print("__SENTINEL__" + json.dumps(result, ensure_ascii=False))
PY
'''
    return template.replace("__SENTINEL__", SENTINEL)


def render_bootstrap_host_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
container="$1"
port="$2"
image="$3"
workdir="$4"
pubkey="$5"

emit_json() {
  python3 - "$1" <<'PY'
import json
import sys
print("__SENTINEL__" + json.dumps(json.loads(sys.argv[1]), ensure_ascii=False))
PY
}

if ! command -v docker >/dev/null 2>&1; then
  emit_json '{"success": false, "error": "docker not found on host", "phase": "probe"}'
  exit 10
fi
if ! docker info >/dev/null 2>&1; then
  emit_json '{"success": false, "error": "docker info failed on host", "phase": "probe"}'
  exit 11
fi

missing=()
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
  [ -e "$p" ] || missing+=("$p")
done
if [ "${#missing[@]}" -gt 0 ]; then
  missing_json=$(python3 - <<'PY' "${missing[@]}"
import json
import sys
print(json.dumps(list(sys.argv[1:]), ensure_ascii=False))
PY
)
  emit_json "{\"success\": false, \"error\": \"required host paths missing\", \"phase\": \"probe\", \"missing\": ${missing_json}}"
  exit 12
fi

actions=()
created=false
started=false
pulled=false
installed_ssh=false
firewall="none"

if docker inspect "$container" >/dev/null 2>&1; then
  state=$(docker inspect -f '{{.State.Status}}' "$container")
  if [ "$state" != "running" ]; then
    docker start "$container" >/dev/null
    started=true
    actions+=("started-existing-container")
  fi
else
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    pull_log=$(mktemp)
    if ! docker pull "$image" >"$pull_log" 2>&1; then
      tail -n 120 "$pull_log" >&2 || true
      rm -f "$pull_log"
      emit_json '{"success": false, "error": "docker pull failed", "phase": "image"}'
      exit 20
    fi
    rm -f "$pull_log"
    pulled=true
    actions+=("pulled-image")
  fi

  mount_args=()
  for optional in /home /tmp /weight /data /mnt; do
    if [ -e "$optional" ]; then
      mount_args+=("-v" "$optional:$optional")
    fi
  done

  docker run --name "$container" -it -d --network host --shm-size=500g \
    --privileged=true \
    --label com.vaws.managed=true \
    --label com.vaws.container_ssh_port="$port" \
    --label com.vaws.workdir="$workdir" \
    -w "$workdir" \
    --device=/dev/davinci_manager \
    --device=/dev/hisi_hdc \
    --device=/dev/devmm_svm \
    --entrypoint=bash \
    -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    -v /usr/local/dcmi:/usr/local/dcmi \
    -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    -v /usr/local/sbin:/usr/local/sbin \
    -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime:ro \
    "${mount_args[@]}" \
    "$image" >/dev/null

  created=true
  actions+=("created-container")
fi

if ! docker exec "$container" bash -lc 'command -v sshd >/dev/null 2>&1 && command -v ssh >/dev/null 2>&1'; then
  if ! docker exec "$container" bash -lc '
set -euo pipefail
log=$(mktemp)
trap "rm -f \"$log\"" EXIT
export DEBIAN_FRONTEND=noninteractive
if ! (apt-get -o Acquire::Check-Date=false -o Acquire::Check-Valid-Until=false update >"$log" 2>&1 && apt-get install -y openssh-server openssh-client --fix-missing >>"$log" 2>&1); then
  tail -n 120 "$log" >&2 || true
  exit 97
fi
'; then
    emit_json '{"success": false, "error": "installing openssh inside container failed", "phase": "container-ssh"}'
    exit 30
  fi
  installed_ssh=true
  actions+=("installed-openssh")
fi

if ! docker exec -i "$container" bash -s -- "$port" "$pubkey" <<'INNER'
set -euo pipefail
port="$1"
pubkey="$2"
install -d -m 700 /root/.ssh
[ -f /root/.ssh/authorized_keys ] || touch /root/.ssh/authorized_keys
if ! grep -qxF "$pubkey" /root/.ssh/authorized_keys 2>/dev/null; then
  printf '%s\n' "$pubkey" >> /root/.ssh/authorized_keys
fi
chmod 600 /root/.ssh/authorized_keys
ssh-keygen -A >/dev/null 2>&1 || true
cat > /etc/ssh/sshd_vaws_config <<EOF
Port ${port}
ListenAddress 0.0.0.0
Protocol 2
HostKey /etc/ssh/ssh_host_rsa_key
HostKey /etc/ssh/ssh_host_ecdsa_key
HostKey /etc/ssh/ssh_host_ed25519_key
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
UsePAM no
PrintMotd no
AuthorizedKeysFile .ssh/authorized_keys
PidFile /run/sshd_vaws.pid
EOF
/usr/sbin/sshd -t -f /etc/ssh/sshd_vaws_config
if [ -f /run/sshd_vaws.pid ]; then
  kill "$(cat /run/sshd_vaws.pid)" 2>/dev/null || true
  rm -f /run/sshd_vaws.pid
fi
pkill -f '/etc/ssh/sshd_vaws_config' 2>/dev/null || true
/usr/sbin/sshd -f /etc/ssh/sshd_vaws_config
if command -v ss >/dev/null 2>&1; then
  ss -ltnH | awk '{print $4}' | grep -Eq "[:.]${port}$"
fi
INNER
then
  emit_json '{"success": false, "error": "configuring dedicated container sshd failed", "phase": "container-ssh"}'
  exit 31
fi
actions+=("configured-dedicated-sshd")

if command -v ufw >/dev/null 2>&1; then
  ufw allow "${port}/tcp" >/dev/null 2>&1 || true
  firewall="ufw"
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --quiet --add-port="${port}/tcp" --permanent >/dev/null 2>&1 || true
  firewall-cmd --quiet --reload >/dev/null 2>&1 || true
  firewall="firewalld"
fi

payload=$(python3 - <<'PY' "$container" "$port" "$image" "$workdir" "$created" "$started" "$pulled" "$installed_ssh" "$firewall" "${actions[*]}"
import json
import sys
container, port, image, workdir, created, started, pulled, installed_ssh, firewall, actions = sys.argv[1:]
print(json.dumps({
    "success": True,
    "container": container,
    "container_ssh_port": int(port),
    "image": image,
    "workdir": workdir,
    "created": created == "true",
    "started_existing": started == "true",
    "pulled_image": pulled == "true",
    "installed_openssh": installed_ssh == "true",
    "firewall": firewall,
    "actions": [item for item in actions.split() if item],
}, ensure_ascii=False))
PY
)
emit_json "$payload"
'''
    return template.replace("__SENTINEL__", SENTINEL)


def render_smoke_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
requested_python="${1:-}"

detect_python() {
  if [ -n "$requested_python" ] && [ -x "$requested_python" ]; then
    printf '%s\n' "$requested_python"
    return 0
  fi
  while IFS= read -r candidate; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done < <(ls -1d /usr/local/python*/bin/python3 2>/dev/null | sort -V -r)
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  return 1
}

PYTHON_BIN="$(detect_python || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "__SENTINEL__{\"success\": false, \"error\": \"python not found in container\", \"phase\": \"python-discovery\"}"
  exit 40
fi

export PATH="${PATH:-}"
export LD_LIBRARY_PATH="/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64:${LD_LIBRARY_PATH:-}"
export VLLM_WORKER_MULTIPROC_METHOD=spawn
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1

sourced_file_log=""
safe_source() {
  file="$1"
  if [ -f "$file" ]; then
    set +u
    . "$file" >/dev/null 2>&1 || true
    set -u
    sourced_file_log+="$file\n"
  fi
}

safe_source /usr/local/Ascend/ascend-toolkit/set_env.sh
safe_source /usr/local/Ascend/ascend-toolkit/latest/set_env.sh
safe_source /usr/local/Ascend/nnal/atb/set_env.sh
safe_source /vllm-workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash

"$PYTHON_BIN" - <<'PY' "$PYTHON_BIN" "$sourced_file_log"
import json
import os
import sys
import traceback

python_path = sys.argv[1]
sourced_text = sys.argv[2]
result = {
    "success": False,
    "python_path": python_path,
    "python_version": sys.version.split()[0],
    "driver_ld_library_path_prefix": [
        "/usr/local/Ascend/driver/lib64/driver",
        "/usr/local/Ascend/driver/lib64",
    ],
    "ld_library_path": os.environ.get("LD_LIBRARY_PATH", ""),
    "sourced_scripts": [line for line in sourced_text.splitlines() if line],
}
try:
    import torch
    import torch_npu  # noqa: F401

    x = torch.zeros(1, 2).npu()
    result.update(
        {
            "success": True,
            "torch_version": getattr(torch, "__version__", None),
            "shape": list(x.shape),
            "device": str(x.device),
        }
    )
    if not str(x.device).startswith("npu"):
        raise RuntimeError(f"unexpected device: {x.device}")
except Exception as exc:  # pragma: no cover - runtime dependent
    tb = traceback.format_exc().splitlines()
    result.update(
        {
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback_tail": tb[-25:],
        }
    )
print("__SENTINEL__" + json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result["success"] else 3)
PY
'''
    return template.replace("__SENTINEL__", SENTINEL)


def render_mesh_export_key_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
comment="$1"
install -d -m 700 /root/.ssh
if [ ! -f /root/.ssh/id_ed25519 ]; then
  ssh-keygen -t ed25519 -C "$comment" -f /root/.ssh/id_ed25519 -N "" >/dev/null
fi
pubkey="$(cat /root/.ssh/id_ed25519.pub)"
fingerprint="$(ssh-keygen -lf /root/.ssh/id_ed25519.pub | awk '{print $2}')"
python3 - <<'PY' "$comment" "$pubkey" "$fingerprint"
import json
import sys
print("__SENTINEL__" + json.dumps({
    "success": True,
    "comment": sys.argv[1],
    "public_key": sys.argv[2],
    "fingerprint": sys.argv[3],
}, ensure_ascii=False))
PY
'''
    return template.replace("__SENTINEL__", SENTINEL)


def render_mesh_add_peer_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
peer_key="$1"
peer_host="$2"
peer_port="$3"
install -d -m 700 /root/.ssh
[ -f /root/.ssh/authorized_keys ] || touch /root/.ssh/authorized_keys
[ -f /root/.ssh/known_hosts ] || touch /root/.ssh/known_hosts
if ! grep -qxF "$peer_key" /root/.ssh/authorized_keys 2>/dev/null; then
  printf '%s\n' "$peer_key" >> /root/.ssh/authorized_keys
fi
chmod 600 /root/.ssh/authorized_keys
ssh-keyscan -p "$peer_port" "$peer_host" >> /root/.ssh/known_hosts 2>/dev/null || true
python3 - <<'PY' "$peer_host" "$peer_port" "$peer_key"
import json
import sys
parts = sys.argv[3].split()
print("__SENTINEL__" + json.dumps({
    "success": True,
    "peer_host": sys.argv[1],
    "peer_port": int(sys.argv[2]),
    "peer_key_added": True,
    "peer_key_comment": parts[-1] if parts else None,
}, ensure_ascii=False))
PY
'''
    return template.replace("__SENTINEL__", SENTINEL)


def render_mesh_remove_peer_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
peer_comment="$1"
peer_host="$2"
peer_port="$3"
removed=0
if [ -f /root/.ssh/authorized_keys ]; then
  tmp="$(mktemp)"
  before="$(wc -l < /root/.ssh/authorized_keys 2>/dev/null || echo 0)"
  grep -vF "$peer_comment" /root/.ssh/authorized_keys > "$tmp" || true
  cat "$tmp" > /root/.ssh/authorized_keys
  rm -f "$tmp"
  chmod 600 /root/.ssh/authorized_keys
  after="$(wc -l < /root/.ssh/authorized_keys 2>/dev/null || echo 0)"
  removed=$(( before - after ))
fi
if [ -f /root/.ssh/known_hosts ]; then
  ssh-keygen -R "[${peer_host}]:${peer_port}" -f /root/.ssh/known_hosts >/dev/null 2>&1 || true
fi
python3 - <<'PY' "$peer_comment" "$peer_host" "$peer_port" "$removed"
import json
import sys
print("__SENTINEL__" + json.dumps({
    "success": True,
    "peer_comment": sys.argv[1],
    "peer_host": sys.argv[2],
    "peer_port": int(sys.argv[3]),
    "removed_authorized_key_lines": int(sys.argv[4]),
}, ensure_ascii=False))
PY
'''
    return template.replace("__SENTINEL__", SENTINEL)


def render_remove_container_host_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
container="$1"
if ! command -v docker >/dev/null 2>&1; then
  echo "__SENTINEL__{\"success\": false, \"error\": \"docker not found on host\"}"
  exit 10
fi
if docker inspect "$container" >/dev/null 2>&1; then
  state="$(docker inspect -f '{{.State.Status}}' "$container")"
  docker rm -f "$container" >/dev/null
  python3 - <<'PY' "$container" "$state"
import json
import sys
print("__SENTINEL__" + json.dumps({
    "success": True,
    "container": sys.argv[1],
    "removed": True,
    "previous_state": sys.argv[2],
}, ensure_ascii=False))
PY
else
  python3 - <<'PY' "$container"
import json
import sys
print("__SENTINEL__" + json.dumps({
    "success": True,
    "container": sys.argv[1],
    "removed": False,
    "previous_state": None,
}, ensure_ascii=False))
PY
fi
'''
    return template.replace("__SENTINEL__", SENTINEL)


def container_target(args: argparse.Namespace) -> SshTarget:
    return SshTarget(host=args.host, user="root", port=args.container_ssh_port)


def host_target(args: argparse.Namespace) -> SshTarget:
    return SshTarget(host=args.host, user=args.user, port=args.host_port)


def check_direct_ssh(target: SshTarget) -> dict[str, Any]:
    result = run_local(ssh_command(target, batch_mode=True) + ["printf", "ok"])
    return {
        "ok": result.returncode == 0 and result.stdout.endswith("ok"),
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def cmd_probe_host(args: argparse.Namespace) -> int:
    target = host_target(args)
    result = run_remote_script(
        target,
        render_host_probe_script(),
        args=[args.image, args.port_range, args.managed_prefix],
        batch_mode=True,
    )
    payload = assert_remote_success(result)
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "port": target.port,
    }
    print_json(payload)
    return 0


def cmd_bootstrap_container(args: argparse.Namespace) -> int:
    target = host_target(args)
    key_path = find_public_key(args.public_key_file)
    public_key = load_public_key(key_path)
    result = run_remote_script(
        target,
        render_bootstrap_host_script(),
        args=[
            args.container_name,
            str(args.container_ssh_port),
            args.image,
            args.workdir,
            public_key,
        ],
        batch_mode=True,
    )
    payload = assert_remote_success(result)

    ssh_check = check_direct_ssh(
        SshTarget(host=args.host, user="root", port=args.container_ssh_port)
    )
    payload.update(
        {
            "public_key_file": str(key_path),
            "direct_container_ssh": ssh_check,
            "target": {
                "host": target.host,
                "user": target.user,
                "host_port": target.port,
                "container_ssh_port": args.container_ssh_port,
            },
        }
    )
    if not ssh_check["ok"]:
        raise MachineManagementError(json.dumps(payload, ensure_ascii=False))

    print_json(payload)
    return 0


def smoke_payload(args: argparse.Namespace) -> dict[str, Any]:
    target = container_target(args)
    python_arg = args.python if args.python else ""
    result = run_remote_script(
        target,
        render_smoke_script(),
        args=[python_arg],
        batch_mode=True,
    )
    payload = assert_remote_success(result, require_payload=True)
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "container_ssh_port": target.port,
    }
    return payload


def cmd_smoke(args: argparse.Namespace) -> int:
    payload = smoke_payload(args)
    print_json(payload)
    return 0


def cmd_verify_machine(args: argparse.Namespace) -> int:
    host = host_target(args)
    container = container_target(args)
    host_check = check_direct_ssh(host)
    container_check = check_direct_ssh(container)
    result: dict[str, Any] = {
        "host": {"host": host.host, "user": host.user, "port": host.port},
        "container": {"host": container.host, "user": container.user, "port": container.port},
        "host_ssh": host_check,
        "container_ssh": container_check,
    }
    if container_check["ok"]:
        smoke_args = argparse.Namespace(
            host=args.host,
            user=args.user,
            container_ssh_port=args.container_ssh_port,
            python=args.python,
        )
        try:
            result["smoke"] = smoke_payload(smoke_args)
        except MachineManagementError as exc:
            result["smoke"] = {"success": False, "error": str(exc)}
    else:
        result["smoke"] = {"success": False, "skipped": "container SSH failed"}

    result["ready"] = bool(
        host_check["ok"]
        and container_check["ok"]
        and result["smoke"].get("success") is True
    )
    print_json(result)
    return 0


def cmd_mesh_export_key(args: argparse.Namespace) -> int:
    target = container_target(args)
    result = run_remote_script(
        target,
        render_mesh_export_key_script(),
        args=[args.comment],
        batch_mode=True,
    )
    payload = assert_remote_success(result)
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "container_ssh_port": target.port,
    }
    print_json(payload)
    return 0


def cmd_mesh_add_peer(args: argparse.Namespace) -> int:
    target = container_target(args)
    result = run_remote_script(
        target,
        render_mesh_add_peer_script(),
        args=[args.peer_public_key, args.peer_host, str(args.peer_port)],
        batch_mode=True,
    )
    payload = assert_remote_success(result)
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "container_ssh_port": target.port,
    }
    print_json(payload)
    return 0


def cmd_mesh_remove_peer(args: argparse.Namespace) -> int:
    target = container_target(args)
    result = run_remote_script(
        target,
        render_mesh_remove_peer_script(),
        args=[args.peer_comment, args.peer_host, str(args.peer_port)],
        batch_mode=True,
    )
    payload = assert_remote_success(result)
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "container_ssh_port": target.port,
    }
    print_json(payload)
    return 0


def remove_known_host_entry(host: str, port: int, known_hosts: pathlib.Path) -> dict[str, Any]:
    if not known_hosts.exists():
        return {
            "success": True,
            "known_hosts": str(known_hosts),
            "removed": False,
            "reason": "known_hosts file does not exist",
        }
    proc = run_local(
        ["ssh-keygen", "-R", f"[{host}]:{port}", "-f", str(known_hosts)]
    )
    combined = (proc.stdout + "\n" + proc.stderr).lower()
    return {
        "success": proc.returncode == 0 or "not found" in combined,
        "known_hosts": str(known_hosts),
        "removed": proc.returncode == 0,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def cmd_clean_local_known_hosts(args: argparse.Namespace) -> int:
    known_hosts = pathlib.Path(args.known_hosts).expanduser().resolve()
    payload = remove_known_host_entry(args.host, args.container_ssh_port, known_hosts)
    payload.update({"host": args.host, "container_ssh_port": args.container_ssh_port})
    print_json(payload)
    return 0


def cmd_remove_container(args: argparse.Namespace) -> int:
    target = host_target(args)
    result = run_remote_script(
        target,
        render_remove_container_host_script(),
        args=[args.container_name],
        batch_mode=True,
    )
    payload = assert_remote_success(result)
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "host_port": target.port,
    }
    if args.clean_local_known_hosts and args.container_ssh_port:
        payload["local_known_hosts_cleanup"] = remove_known_host_entry(
            args.host,
            args.container_ssh_port,
            pathlib.Path(args.known_hosts).expanduser().resolve(),
        )
    print_json(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_host_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", required=True, help="host IP or DNS name")
        p.add_argument("--user", default=DEFAULT_HOST_USER, help=f"SSH user (default: {DEFAULT_HOST_USER})")
        p.add_argument("--host-port", type=int, default=DEFAULT_HOST_PORT, help=f"host SSH port (default: {DEFAULT_HOST_PORT})")

    def add_container_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", required=True, help="host IP or DNS name")
        p.add_argument("--user", default=DEFAULT_HOST_USER, help=f"SSH user (default: {DEFAULT_HOST_USER})")
        p.add_argument("--container-ssh-port", type=int, required=True, help="direct SSH port exposed by the managed container")

    probe = subparsers.add_parser("probe-host", help="probe host prerequisites and choose a free high SSH port")
    add_host_args(probe)
    probe.add_argument("--image", default=DEFAULT_IMAGE, help=f"image name (default: {DEFAULT_IMAGE})")
    probe.add_argument("--port-range", default=DEFAULT_PORT_RANGE, help=f"inclusive range START:END (default: {DEFAULT_PORT_RANGE})")
    probe.add_argument("--managed-prefix", default="vaws-", help="managed container name prefix (default: vaws-)")
    probe.set_defaults(func=cmd_probe_host)

    bootstrap = subparsers.add_parser("bootstrap-container", help="create or repair the managed container and dedicated sshd")
    add_host_args(bootstrap)
    bootstrap.add_argument("--container-name", required=True, help="container name to create or reuse")
    bootstrap.add_argument("--container-ssh-port", type=int, required=True, help="high non-default SSH port for the managed container")
    bootstrap.add_argument("--image", default=DEFAULT_IMAGE, help=f"image name (default: {DEFAULT_IMAGE})")
    bootstrap.add_argument("--workdir", default=DEFAULT_WORKDIR, help=f"container workdir (default: {DEFAULT_WORKDIR})")
    bootstrap.add_argument("--public-key-file", help="local SSH public key to add to the container; defaults to ~/.ssh/id_ed25519.pub if present")
    bootstrap.set_defaults(func=cmd_bootstrap_container)

    smoke = subparsers.add_parser("smoke", help="run the container-side torch/torch_npu smoke test with dynamic env discovery")
    add_container_args(smoke)
    smoke.add_argument("--python", help="optional explicit python path inside the container")
    smoke.set_defaults(func=cmd_smoke)

    verify = subparsers.add_parser("verify-machine", help="check host SSH, container SSH, and smoke readiness together")
    add_container_args(verify)
    verify.add_argument("--host-port", type=int, default=DEFAULT_HOST_PORT, help=f"host SSH port (default: {DEFAULT_HOST_PORT})")
    verify.add_argument("--python", help="optional explicit python path inside the container")
    verify.set_defaults(func=cmd_verify_machine)

    mesh_export = subparsers.add_parser("mesh-export-key", help="ensure a mesh key exists in the container and print its public key")
    add_container_args(mesh_export)
    mesh_export.add_argument("--comment", required=True, help="stable mesh key comment, for example vaws-mesh:173.125.1.2")
    mesh_export.set_defaults(func=cmd_mesh_export_key)

    mesh_add = subparsers.add_parser("mesh-add-peer", help="add a peer mesh key and known_hosts entry inside a managed container")
    add_container_args(mesh_add)
    mesh_add.add_argument("--peer-public-key", required=True, help="peer public key line, including its comment")
    mesh_add.add_argument("--peer-host", required=True, help="peer host IP or DNS name")
    mesh_add.add_argument("--peer-port", type=int, required=True, help="peer container SSH port")
    mesh_add.set_defaults(func=cmd_mesh_add_peer)

    mesh_remove = subparsers.add_parser("mesh-remove-peer", help="remove a peer mesh key and known_hosts entry inside a managed container")
    add_container_args(mesh_remove)
    mesh_remove.add_argument("--peer-comment", required=True, help="stable mesh comment, for example vaws-mesh:173.125.1.2")
    mesh_remove.add_argument("--peer-host", required=True, help="peer host IP or DNS name")
    mesh_remove.add_argument("--peer-port", type=int, required=True, help="peer container SSH port")
    mesh_remove.set_defaults(func=cmd_mesh_remove_peer)

    clean_known_hosts = subparsers.add_parser("clean-local-known-hosts", help="remove one managed container endpoint from local known_hosts")
    clean_known_hosts.add_argument("--host", required=True, help="host IP or DNS name")
    clean_known_hosts.add_argument("--container-ssh-port", type=int, required=True, help="managed container SSH port")
    clean_known_hosts.add_argument("--known-hosts", default=str(DEFAULT_KNOWN_HOSTS), help=f"known_hosts path (default: {DEFAULT_KNOWN_HOSTS})")
    clean_known_hosts.set_defaults(func=cmd_clean_local_known_hosts)

    remove = subparsers.add_parser("remove-container", help="remove a managed container from the host")
    add_host_args(remove)
    remove.add_argument("--container-name", required=True, help="container name to remove")
    remove.add_argument("--container-ssh-port", type=int, help="container SSH port for optional local known_hosts cleanup")
    remove.add_argument("--known-hosts", default=str(DEFAULT_KNOWN_HOSTS), help=f"known_hosts path (default: {DEFAULT_KNOWN_HOSTS})")
    remove.add_argument(
        "--clean-local-known-hosts",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="also remove the endpoint from local known_hosts when --container-ssh-port is provided",
    )
    remove.set_defaults(func=cmd_remove_container)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MachineManagementError as exc:
        print_json({"success": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
