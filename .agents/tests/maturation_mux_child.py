"""A real local CLI child using the exact provider through a fake transport."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

payload = json.loads(sys.stdin.read())
os.environ["REMOTE_DEV_SSH_MUX_DIR"] = payload["mux_dir"]
sys.path.insert(0, sys.argv[1])
from core.endpoint import Endpoint
import core.ssh_transport as transport

calls = []

def fake_run(argv, **kwargs):
    calls.append({"argv": argv, "input": repr(kwargs.get("input"))})
    output = '{"status":"ok","fake_transport":true}'
    if not kwargs.get("text"):
        output = output.encode()
    return subprocess.CompletedProcess(argv, 0, output, "" if kwargs.get("text") else b"")

endpoint = Endpoint(host="acceptance.invalid", user="fixture", port=22222,
                    identity_file="/test-owned/fake-identity")
with mock.patch.object(transport.subprocess, "run", fake_run):
    transport.run_script(endpoint, "printf 'fake only'\n", timeout_ms=700)
    transport.run_bytes(endpoint, "cat 'path with spaces'", stdin=b"local fixture", timeout_ms=700)
    transport.run_remote_python(endpoint, "import json\nprint(json.dumps({'status':'ok'}))", {"fixture": True}, timeout_ms=700)
trace = {"pid": os.getpid(), "override": os.environ.get("REMOTE_DEV_SSH_MUX"),
         "mux_ready": transport._MUX_READY, "calls": calls}
Path(payload["trace"]).write_text(json.dumps(trace))
if payload.get("gate"):
    deadline = time.monotonic() + 5
    while not Path(payload["gate"]).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("test-owned gate was not released")
        time.sleep(0.005)
print(json.dumps({"result": {"status": "ok", "trace": trace}}))
