"""Local task facade for the shared coordinator and remote-dev substrate.

Only remote execution constructs a coordinator client. Local identity, source
binding and native resume do not depend on a machine or network connection.
"""
from __future__ import annotations

import fcntl
import contextlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from vaws_agent_session import AgentSessions, load_context
from vaws_local_state import ROOT
from vaws_build_inputs import BUILD_INPUT_ENV_KEYS

DONE = {"succeeded", "failed", "timeout", "cancelled", "inconclusive"}


class CoordinatorClient:
    def __init__(self, state_dir: Path):
        path = state_dir.parent / "coordinator-client.json"
        config = json.loads(path.read_text()) if path.exists() else {}
        self.url = os.environ.get("VAWS_COORDINATOR_URL") or config.get("url", "")
        token = os.environ.get(config.get("token_env", "VAWS_COORDINATOR_TOKEN"), "")
        if not token and config.get("token_file"):
            secret = Path(config["token_file"]).expanduser()
            if secret.stat().st_mode & 0o077:
                raise ValueError("coordinator token file must be private (chmod 600)")
            token = secret.read_text().strip()
        if not self.url or not token:
            raise RuntimeError("remote coordinator is not configured; local development remains available")
        parsed = urlsplit(self.url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("use HTTPS or an authenticated tunnel to a loopback MCP endpoint")
        self.headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"}
        self.sequence = 0
        result = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                        "clientInfo": {"name": "vaws-task", "version": "1"}})
        self.headers["MCP-Protocol-Version"] = result["protocolVersion"]
        request = urllib.request.Request(self.url, data=json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(), headers=self.headers, method="POST")
        with urllib.request.urlopen(request, timeout=15):
            pass

    def rpc(self, method, params):
        self.sequence += 1
        data = json.dumps({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params}).encode()
        request = urllib.request.Request(self.url, data=data, headers=self.headers, method="POST")
        with urllib.request.urlopen(request, timeout=180) as response:
            if response.headers.get("Mcp-Session-Id"):
                self.headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
            payload = json.load(response)
        if "error" in payload:
            raise RuntimeError(payload["error"].get("message", "coordinator RPC failed"))
        return payload["result"]

    def call(self, name, **arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError("; ".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"))
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])


class TaskClient:
    def __init__(self, context_file="", *, client_factory=CoordinatorClient):
        self.context = load_context(context_file)
        self.store = AgentSessions(Path(self.context["state_dir"]))
        self.client_factory = client_factory

    def status(self):
        context = self.store.context(self.context["attachment"]["id"])
        with self.store.transaction() as db:
            attachments = [row for row in self.store.rows(db, "attachment") if row["session_id"] == context["session"]["id"]]
        return {**context, "attachments": attachments, "executions": self.store.executions(context["session"]["id"])}

    def sources(self, sources):
        self.context = self.store.bind_sources(self.context, sources)
        return self.context

    @contextlib.contextmanager
    def execution_lock(self, execution_id):
        # Ids come from the registry; reject path components before opening a
        # lock even for a caller-supplied status/stop id.
        if len(execution_id) != 64 or any(char not in "0123456789abcdef" for char in execution_id):
            raise ValueError("invalid local execution id")
        with (self.store.state_dir / ("execution-" + execution_id + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def run(self, request_id, command, profile_key="", runtime_id="", devices=None, npu_count=1,
            env=None, timeout_seconds=1800):
        if not request_id or len(request_id) > 128:
            raise ValueError("use one stable request_id per intended execution; retain it when retrying")
        spec = {"command": command, "profile_key": profile_key, "runtime_id": runtime_id,
                "devices": devices or [], "npu_count": 0 if devices else npu_count,
                "env": env or {}, "timeout_seconds": timeout_seconds}
        row = self.store.execution(self.context, request_id, spec)
        with self.execution_lock(row["id"]):
            with self.store.transaction() as db:
                row = self.store.get(db, "execution", row["id"])
            if row["phase"] in DONE:
                return {"execution_id": row["id"], "state": row["phase"], **row.get("observation", {})}
            client = self.client_factory(self.store.state_dir)
            if row.get("managed_job"):
                return self.observe(row["id"], client=client, _locked=True)
            context = self.store.context(self.context["attachment"]["id"])
            sources = {name: source["path"] for name, source in context["session"]["sources"].items()}
            if not {"vllm", "vllm-ascend"}.issubset(sources):
                raise ValueError("bind the actual vllm and vllm-ascend worktrees before an Ascend execution")
            if "remote_session" not in row:
                row["remote_session"] = client.call("session_open", session_id=context["session"]["id"], sources=sources)
                self.store.save_execution(row)
            if "binding" not in row:
                if not profile_key:
                    candidates = [item for item in client.call("runtime_catalog")["runtimes"]
                                  if item["state"] == "ready" and (not runtime_id or item["runtime_id"] == runtime_id)]
                    profiles = {item["profile_key"] for item in candidates}
                    if len(profiles) != 1:
                        return {"state": "waiting_for_runtime", "execution_id": row["id"], "provisioning_started": False,
                                "reason": "no unique ready profile; select the required environment"}
                    profile_key = next(iter(profiles))
                binding = client.call("runtime_checkout", session=row["remote_session"]["id"], profile_key=profile_key,
                                      request_id=row["id"], runtime_id=runtime_id)
                if binding.get("status") == "cache_miss":
                    return {**binding, "state": "waiting_for_runtime", "execution_id": row["id"]}
                row.update(binding=binding, phase="bound", sources=sources)
                self.store.save_execution(row)
            if "snapshots" not in row:
                row["snapshots"] = self._sync(row)
                row["phase"] = "launch_pending"
                self.store.save_execution(row)
            # Persist snapshots before this call. A lost reply retries the same
            # command/lease/job ids without re-syncing underneath a running job.
            binding = row["binding"]
            job = client.call("managed_execution_start", binding_id=binding["id"], request_id=row["id"],
                              snapshots=row["snapshots"], expected_build_key=binding["build_key"],
                              command=command, env=env or {}, devices=spec["devices"], npu_count=spec["npu_count"],
                              timeout_seconds=timeout_seconds)
            row.update(managed_job=job["id"], phase=job["state"], observation=job)
            self.store.save_execution(row)
            return {"execution_id": row["id"], **job}

    def _sync(self, row):
        binding = row["binding"]
        endpoint = binding["endpoint"]
        directory = self.store.state_dir / "runs" / row["id"]
        directory.mkdir(parents=True, exist_ok=True)
        args = [sys.executable, str(ROOT / ".agents/skills/remote-code-parity/scripts/remote_code_parity.py"),
                "sync", "--workspace-root", str(ROOT), "--workspace-id", self.context["session"]["id"],
                "--server-name", binding["runtime_id"], "--runtime-root", endpoint["root"],
                "--container-identity", binding["runtime_id"], "--container-host", endpoint["host"],
                "--container-port", str(endpoint["port"]), "--container-user", endpoint["user"],
                "--apply-mode", "materialize"]
        for name in ("vllm", "vllm-ascend"):
            args.extend(["--source", name + "=" + row["sources"][name]])
        environment = {key: value for key, value in os.environ.items() if key not in BUILD_INPUT_ENV_KEYS}
        environment.update(binding.get("build_env", {}))
        environment.update(binding["environment"])
        with (directory / "parity.json").open("w") as stdout, (directory / "parity.log").open("w") as stderr:
            result = subprocess.run(args, stdout=stdout, stderr=stderr,
                                    env=environment, timeout=600, check=False)
        if result.returncode:
            raise RuntimeError(f"source synchronization failed; no job launched; inspect {directory / 'parity.log'}")
        payload = json.loads((directory / "parity.json").read_text())
        if payload.get("status") not in {"ready", "materialized"}:
            raise RuntimeError("source staging alone does not authorize execution")
        return payload["snapshot_commits"]

    def observe(self, execution_id, action="status", force=False, *, client=None, _locked=False):
        if not _locked:
            with self.execution_lock(execution_id):
                return self.observe(execution_id, action, force, client=client, _locked=True)
        with self.store.transaction() as db:
            row = self.store.get(db, "execution", execution_id)
        if row["session_id"] != self.context["session"]["id"]:
            raise PermissionError("execution belongs to another VAWS task")
        if not row.get("managed_job"):
            if row.get("phase") == "launch_pending":
                client = client or self.client_factory(self.store.state_dir)
                matches = [job for job in client.call("coordinator_status")["jobs"]
                           if job["binding_id"] == row["binding"]["id"]
                           and job["request"]["request_id"] == row["id"]]
                if len(matches) == 1:
                    row["managed_job"] = matches[0]["id"]
                    self.store.save_execution(row)
                    return self.observe(execution_id, action, force, client=client, _locked=True)
                if action != "stop":
                    raise RuntimeError("launch outcome requires retrying the same vaws_run request_id")
                # A late start against a returned binding is rejected by the
                # coordinator. Do not resync or create a substitute execution.
            if action == "stop" and row.get("binding"):
                client = client or self.client_factory(self.store.state_dir)
                client.call("runtime_return", binding_id=row["binding"]["id"])
                row["phase"] = "cancelled"
                self.store.save_execution(row)
            elif action == "stop":
                row["phase"] = "cancelled"
                self.store.save_execution(row)
            return {"execution_id": row["id"], "state": row["phase"]}
        client = client or self.client_factory(self.store.state_dir)
        job = client.call("managed_execution_control", job_id=row["managed_job"], action=action, force=force)
        row.update(phase=job["state"], observation=job)
        self.store.save_execution(row)
        return {"execution_id": row["id"], **job}

    def finish(self, force=False):
        with self.store.transaction() as db:
            session = self.store.get(db, "session", self.context["session"]["id"])
            session["state"] = "finishing"
            self.store.put(db, "session", session)
        executions = self.store.executions(self.context["session"]["id"])
        states = []
        for row in executions:
            if row["phase"] in DONE:
                continue
            states.append(self.observe(row["id"], "stop", force))
        finished = all(row["state"] in DONE for row in states)
        with self.store.transaction() as db:
            session = self.store.get(db, "session", self.context["session"]["id"])
            session["state"] = "finished" if finished else "finishing"
            self.store.put(db, "session", session)
        return {"state": session["state"], "executions": states, "worktrees_preserved": True}
