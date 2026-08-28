"""Adapters to the existing remote-dev substrate and host NPU coordinator."""
from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Keep the installed MCP SDK ahead of remote-dev's unrelated `mcp/` package.
sys.path.append(str(ROOT / ".remote-dev"))
sys.path.insert(0, str(ROOT / ".agents/lib"))
from core.endpoint import resolve_endpoint
from core.shell_ops import remote_bash
from vaws_remote_toolbox import _load_inventory
from vaws_runtime_profile import launch_preamble

spec = importlib.util.spec_from_file_location("pool_host_coordination", ROOT / ".agents/skills/session-management/scripts/npu_coordination.py")
coord = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coord)


class RemoteBackend:
    def job(self, runtime, job_id, action, **parameters):
        source = (ROOT / ".remote-dev/core/managed_jobs.py").read_text()
        request = {"root": runtime["endpoint"]["root"], "job_id": job_id, "action": action, **parameters}
        command = ("python3 - " + shlex.quote(json.dumps(request)) + " <<'VAWS_MANAGED_JOB'\n"
                   + "WORKER_SOURCE = " + repr(source)
                   + "\nexec(compile(WORKER_SOURCE, '<vaws-managed-job>', 'exec'))\nVAWS_MANAGED_JOB\n")
        return json.loads(self.bash(runtime["endpoint"], command))

    def job_host_pid(self, runtime, receipt):
        # Container PID namespaces differ from the physical host's allocator.
        # Match both namespace PID and kernel start ticks within this container.
        code = '''
import json, subprocess
from pathlib import Path
request = json.loads(__import__('sys').argv[1])
info = json.loads(subprocess.check_output(['docker','inspect','--format','{{json .}}',request['container_name']], text=True))
if info['Id'] != request['container_id']:
    raise RuntimeError('container identity changed before activation')
receipt = request['receipt']
if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != receipt['boot_id']:
    raise RuntimeError('host boot identity changed')
rows = subprocess.check_output(['docker','top',request['container_name'],'-eo','pid'], text=True).splitlines()[1:]
matches = []
for row in rows:
    pid = int(row.strip())
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ',1)[1].split()
        status = Path(f'/proc/{pid}/status').read_text().splitlines()
        namespace = next(line.split()[1:] for line in status if line.startswith('NSpid:'))
        if int(namespace[-1]) == receipt['pid'] and fields[19] == receipt['start_ticks'] and fields[0] != 'Z':
            matches.append(pid)
    except FileNotFoundError:
        continue
if len(matches) != 1:
    raise RuntimeError('cannot identify a unique host PID for the waiting supervisor')
print(json.dumps({'pid':matches[0]}))
'''
        request = {"container_name": runtime["container_name"], "container_id": runtime["attestation"]["container_id"], "receipt": receipt}
        command = "python3 - " + shlex.quote(json.dumps(request)) + " <<'VAWS_HOST_PID'\n" + code + "\nVAWS_HOST_PID\n"
        return json.loads(self.bash({**runtime["host_endpoint"], "root": "/", "cwd": "/"}, command))["pid"]

    def catalog(self):
        inventory, path = _load_inventory(ROOT)
        return {"inventory_path": str(path), "machines": [
            {"alias": row.get("alias"), "host": row.get("host", {}).get("ip"),
             "container_name": row.get("container", {}).get("name"),
             "container_port": row.get("container", {}).get("ssh_port")}
            for row in inventory["machines"]]}

    def resolve_registration(self, spec):
        if "machine" not in spec:
            return spec
        inventory, _ = _load_inventory(ROOT)
        matches = [row for row in inventory["machines"] if row.get("alias") == spec["machine"]]
        if len(matches) != 1:
            raise ValueError("machine alias must resolve uniquely in the shared inventory")
        host = matches[0]["host"]
        return {"host_endpoint": {"host": host["ip"], "port": host.get("port", 22), "user": host.get("user", "root")},
                "endpoint": {"host": host["ip"], "port": spec["port"], "root": spec["root"], "user": spec.get("user", "root")},
                "container_name": spec["container_name"], "service_ports": spec.get("service_ports", [])}

    def host(self, runtime, request):
        result = coord.ssh_execute(coord.LocalEndpoint(**runtime["host_endpoint"]),
                                  coord.build_remote_command(request), timeout=45)
        if result.returncode:
            raise RuntimeError("host coordination failed; inspect endpoint logs and reconcile before retry")
        payload = json.loads(result.stdout)
        if payload.get("status") in {"failed", "needs_input", "probe_failed"}:
            raise RuntimeError(payload.get("error", "host state unknown"))
        return payload

    @staticmethod
    def bash(target, command):
        result = remote_bash(resolve_endpoint(target), command=command, timeout_ms=45000,
                             runtime_env=False)["result"]
        if result["outcome"] != "success":
            raise RuntimeError("runtime probe failed; see remote-dev state logs")
        return Path(result["refs"]["stdout"]).read_text()

    def inspect(self, runtime, *, idle=False, snapshots=None):
        host = {**runtime["host_endpoint"], "root": "/", "cwd": "/"}
        name = shlex.quote(runtime["container_name"])
        fields = shlex.quote('{"Id":{{json .Id}},"State":{{json .State}}}')
        info = json.loads(self.bash(host, f"docker inspect --format {fields} {name}"))
        if not info["State"]["Running"] or info["State"].get("Paused") or info["State"].get("Restarting"):
            raise RuntimeError("prepared container is not running normally")
        if idle:
            # Docker needs PID to map host ps rows back to the container.
            rows = self.bash(host, f"docker top {name} -eo pid,stat,comm").splitlines()[1:]
            allowed = {"bash", "sh", "sshd", "sshd-session", "sshd-auth", "sleep", "tini", "tail", "cat", "init", "systemd"}
            processes = [row.split(None, 2) for row in rows]
            if not processes or any(len(row) != 3 or not row[0].isdigit() or
                                    (not row[1].startswith("Z") and row[2].strip() not in allowed) for row in processes):
                raise RuntimeError("container is not an idle prepared runtime; inspect its workers")
            if runtime.get("service_ports"):
                listeners = self.bash(host, "ss -H -ltn").splitlines()
                occupied = {int(row.split()[3].rsplit(":", 1)[1]) for row in listeners if len(row.split()) >= 4}
                if occupied.intersection(runtime["service_ports"]):
                    raise RuntimeError("a reserved service port is still listening; resolve its owner before launch")
        module = (ROOT / ".agents/lib/vaws_runtime_profile.py").read_text()
        request = json.dumps({"root": runtime["endpoint"]["root"], "snapshots": snapshots or {}})
        build_source = (ROOT / ".agents/lib/vaws_build_inputs.py").read_text()
        runner = "\n_build_namespace = {}\nexec(" + repr(build_source) + ", _build_namespace)\n" + '''
import subprocess
import sys
args = json.loads(sys.argv[1])
root = Path(args["root"])
manifest = json.loads((root / ".vaws-runtime/ready-profile.json").read_text())
verify(root, manifest)
if _build_namespace["runtime_build_inputs"](root, manifest["profile"], manifest["profile_key"]) != manifest["build_inputs"]:
    raise ValueError("cache miss: installed native artifacts do not match current source inputs")
for name, expected in args["snapshots"].items():
    repo = root / name
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(repo), "diff", "HEAD", "--name-only", "--ignore-submodules=dirty"], text=True)
    untracked = subprocess.check_output(["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"], text=True)
    private = {".vaws-runtime", ".remote-code-parity", "Mooncake"} if name == "." else set()
    extras = [path for path in untracked.split("\\0") if path and path.split("/", 1)[0] not in private]
    if head != expected or dirty.strip() or extras:
        raise ValueError("runtime source differs from pinned snapshot: " + name)
print(json.dumps(manifest))
'''
        command = "python3 - " + shlex.quote(request) + " <<'VAWS_READY_PROBE'\n" + module + runner + "\nVAWS_READY_PROBE\n"
        manifest = json.loads(self.bash(runtime["endpoint"], command))
        return {**manifest, "container_id": info["Id"], "launch_preamble": launch_preamble(manifest["profile"])}
