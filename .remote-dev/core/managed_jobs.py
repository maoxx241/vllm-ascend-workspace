"""Linux remote job receipt protocol used by the VAWS execution supervisor.

This module is sent through the remote-dev transport and has no dependencies.
Preparing a job starts only a waiting supervisor. User code cannot run until
the caller has activated its host lease and explicitly opens the start gate.
"""
from __future__ import annotations

import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    with temporary.open("x") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return {"pid": int(pid), "ppid": int(fields[1]), "pgid": int(fields[2]),
                "start_ticks": fields[19], "state": fields[0]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def owned_processes(receipt):
    """Observe marker ownership and ancestry below the verified subreaper.

    Ancestry also covers setsid/clean-environment daemon children. The worker
    stays alive and adopts orphans until all descendants have been reaped.
    An unowned member of the recorded process group is ambiguous, not free.
    The random marker identifies processes; it is not an access credential.
    """
    if receipt["boot_id"] != boot_id():
        return [], ["boot identity changed"]
    identities, tagged, unknown = {}, set(), []
    marker = ("VAWS_REMOTE_JOB_TOKEN=" + receipt["marker"]).encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        identity = None
        try:
            identity = process_identity(entry.name)
            if not identity or identity["state"] == "Z":
                continue
            identities[identity["pid"]] = identity
            environ = (entry / "environ").read_bytes().split(b"\0")
            if marker in environ:
                tagged.add(identity["pid"])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # Other users' unrelated processes are not this job's family.
            # Readable ancestry still identifies our descendants without env;
            # unowned members of the claimed group become unknown below.
            if identity is None:
                unknown.append(int(entry.name))
    anchor = identities.get(receipt["pid"])
    if receipt.get("supervision") == "subreaper" and anchor and anchor["start_ticks"] == receipt["start_ticks"]:
        family = {anchor["pid"]}
        while True:
            descendants = {pid for pid, row in identities.items() if row["ppid"] in family}
            if descendants.issubset(family):
                break
            family.update(descendants)
        tagged.update(family)
    unknown.extend(pid for pid, row in identities.items() if row["pgid"] == receipt["pgid"] and pid not in tagged)
    return [identities[pid] for pid in sorted(tagged)], unknown


def job_status(directory):
    receipt = read_json(directory / "receipt.json")
    if receipt is None:
        return {"state": "uncertain", "reason": "launch intent exists without a process receipt", "quiet": False}
    processes, unknown = owned_processes(receipt)
    result = read_json(directory / "result.json")
    gate = read_json(directory / "go.json")
    if receipt.get("supervision") == "subreaper" and result is None and not any(
            row["pid"] == receipt["pid"] and row["start_ticks"] == receipt["start_ticks"] for row in processes):
        unknown.append("supervisor lost without a descendant-drained receipt")
    if gate and processes:
        spec = read_json(directory / "spec.json")
        opened = gate.get("opened_at", gate["valid_until"] - 30)
        if time.time() >= opened + spec["timeout_seconds"]:
            # The shell may exit while a background descendant stays alive.
            # Report a timeout so the manager also stops that process family.
            result = {"state": "timeout", "reason": "owned descendants exceeded the execution deadline"}
    if unknown:
        state = "uncertain"
    elif processes:
        state = "running" if gate else "prepared"
    elif result:
        state = result["state"]
    elif (directory / "stop.json").exists():
        state = "cancelled"
    else:
        state = result["state"] if result else "lost_outcome"
    public = {key: value for key, value in receipt.items() if key != "marker"}
    # A random ownership marker is not a credential. The physical host retains
    # the lease while any marked process is alive, even before NPU initialization.
    public["process_guard"] = {"marker": receipt["marker"], "boot_id": receipt["boot_id"]}
    if receipt.get("supervision") == "subreaper":
        # A vanished supervisor cannot prove its clean-environment descendants
        # are gone. Host GC must wait for the manager's verified completion.
        public["process_guard"]["retain_until_release"] = True
    return {"state": state, "quiet": not processes and not unknown, "receipt": public,
            "processes": processes, "unknown": unknown, "result": result,
            "remote_dir": str(directory), "gate_open": bool(gate)}


def worker(directory):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # Linux PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot supervise orphan descendants")
    atomic_json(directory / "supervisor-ready.json", {"pid": os.getpid()})
    spec = read_json(directory / "spec.json")
    deadline = time.time() + 120
    while not (directory / "go.json").exists():
        if (directory / "stop.json").exists() or time.time() >= deadline:
            atomic_json(directory / "result.json", {"state": "cancelled", "reason": "start gate not opened"})
            return
        time.sleep(0.1)
    gate = read_json(directory / "go.json")
    if (directory / "stop.json").exists() or gate["valid_until"] <= time.time():
        atomic_json(directory / "result.json", {"state": "cancelled", "reason": "activation ticket expired"})
        return
    with (directory / "stdout.log").open("ab") as stdout, (directory / "stderr.log").open("ab") as stderr:
        environment = {**os.environ, **spec["env"]}
        child = subprocess.Popen(["bash", "-c", spec["command"]], cwd=spec["cwd"], env=environment,
                                 stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        receipt = read_json(directory / "receipt.json")
        deadline = time.monotonic() + spec["timeout_seconds"]
        stopping_at, terminal = None, None
        while True:
            code = child.poll()
            if code is not None:
                # poll() reaps the direct Popen child first. Then reap adopted
                # orphans without stealing the shell's exit status.
                with contextlib.suppress(ChildProcessError):
                    while os.waitpid(-1, os.WNOHANG)[0]:
                        pass
            stop = read_json(directory / "stop.json")
            if terminal is None and (stop or time.monotonic() >= deadline):
                terminal = "timeout" if time.monotonic() >= deadline else "cancelled"
                stopping_at = time.monotonic()
            if terminal:
                processes, _ = owned_processes(receipt)
                force = bool(stop and stop.get("force")) or time.monotonic() >= stopping_at + 2
                signal_processes(processes, signal.SIGKILL if force else signal.SIGTERM, exclude=os.getpid())
            # This kernel list includes zombies and adopted clean-env children.
            # A process cannot fork again after it has been reaped; only this
            # empty-child boundary allows the supervisor to publish completion.
            children = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").read_text().strip()
            if code is not None and not children:
                break
            time.sleep(0.05)
        result = {"state": terminal or ("succeeded" if code == 0 else "failed"),
                  "exit_code": code, "descendants_drained": True, "finished_at": time.time()}
        atomic_json(directory / "result.json", result)


def signal_processes(processes, sig, *, exclude=None):
    for process in processes:
        if process["pid"] == exclude:
            continue
        current = process_identity(process["pid"])
        if current and current["start_ticks"] == process["start_ticks"]:
            with contextlib.suppress(ProcessLookupError):
                os.kill(process["pid"], sig)


def control_job(request, source):
    root = Path(request["root"]).resolve(strict=True)
    identifier = request["job_id"]
    if not re.fullmatch(r"vaws-[a-f0-9]{64}", identifier):
        raise ValueError("invalid managed job id")
    directory = root / ".vaws-runtime" / "remote-dev" / "jobs" / identifier
    if root not in directory.resolve().parents:
        raise ValueError("managed job directory escapes the runtime root")
    action = request["action"]
    if not directory.exists() and action != "prepare":
        return {"state": "absent", "quiet": True}
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if action == "prepare":
            spec = request["spec"]
            cwd = Path(spec["cwd"]).resolve(strict=True)
            if cwd != root and root not in cwd.parents:
                raise ValueError("managed command cwd escapes the runtime root")
            if not 1 <= spec["timeout_seconds"] <= 86400:
                raise ValueError("managed jobs require a bounded timeout of 1..86400 seconds")
            if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key.startswith("VAWS_REMOTE_JOB_") for key in spec["env"]):
                raise ValueError("invalid or reserved environment variable")
            intent = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
            existing = read_json(directory / "intent.json")
            if existing:
                if existing["digest"] != intent:
                    raise ValueError("managed job id reused with different launch arguments")
                return job_status(directory)
            atomic_json(directory / "intent.json", {"digest": intent})
            atomic_json(directory / "spec.json", spec)
            script = directory / "runner.py"
            script.write_text(source)
            os.chmod(script, 0o600)
            marker = uuid.uuid4().hex
            process = subprocess.Popen(
                [sys.executable, str(script), "--worker", str(directory)],
                env={**os.environ, "VAWS_REMOTE_JOB_TOKEN": marker},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            identity = process_identity(process.pid)
            if not identity or identity["pgid"] != process.pid:
                raise RuntimeError("waiting supervisor has no verified process identity")
            atomic_json(directory / "receipt.json", {**identity, "boot_id": boot_id(), "marker": marker,
                                                      "supervision": "subreaper", "job_id": identifier,
                                                      "prepared_at": time.time()})
            deadline = time.monotonic() + 5
            while not (directory / "supervisor-ready.json").exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("waiting supervisor did not enable descendant supervision")
                time.sleep(0.02)
        elif action == "go":
            status = job_status(directory)
            existing = read_json(directory / "go.json")
            if existing:
                if existing["authorization"] != request["authorization"]:
                    raise ValueError("start gate already belongs to another lease/fence")
                return status
            if status["state"] != "prepared" or (directory / "stop.json").exists():
                raise RuntimeError("job is not a verified waiting supervisor")
            opened = time.time()
            atomic_json(directory / "go.json", {"authorization": request["authorization"], "opened_at": opened, "valid_until": opened + 30})
        elif action == "stop":
            receipt = read_json(directory / "receipt.json")
            if receipt is None:
                return job_status(directory)
            processes, unknown = owned_processes(receipt)
            if unknown:
                return job_status(directory)
            atomic_json(directory / "stop.json", {"at": time.time(), "force": bool(request.get("force"))})
            # Signal only PIDs whose marker and start ticks were just observed.
            # Do not infer ownership from a PID alone or kill a whole container.
            # Keep the subreaper alive while it drains the family and publishes
            # completion. Killing it first would orphan clean-env descendants.
            signal_processes(processes, signal.SIGKILL if request.get("force") else signal.SIGTERM,
                             exclude=receipt["pid"] if receipt.get("supervision") == "subreaper" else None)
        elif action == "tail":
            lines = min(200, max(1, int(request.get("lines", 60))))
            result = job_status(directory)
            for name in ("stdout", "stderr"):
                path = directory / (name + ".log")
                if path.exists():
                    with path.open("rb") as stream:
                        stream.seek(max(0, path.stat().st_size - 32000))
                        result[name] = "\n".join(stream.read().decode(errors="replace").splitlines()[-lines:])
            return result
        elif action != "status":
            raise ValueError("unsupported managed job action")
        return job_status(directory)


if __name__ == "__main__":
    if sys.argv[1] == "--worker":
        worker(Path(sys.argv[2]))
    else:
        print(json.dumps(control_job(json.loads(sys.argv[1]), WORKER_SOURCE)))
