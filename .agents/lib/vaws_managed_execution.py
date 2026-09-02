"""Persisted execution supervision on the existing runtime/host authorities.

This is a bounded job state machine, not a workflow engine. Remote-dev owns
process receipts; RuntimePool owns checkouts; the host owns physical NPU leases.
"""
from __future__ import annotations

import re
import shlex

from vaws_runtime_profile import digest

JOB_TERMINAL = {"succeeded", "failed", "timeout", "cancelled", "inconclusive"}
LEASE_TERMINAL = {"released", "cancelled", "expired"}


class ExecutionRequestError(ValueError):
    """A permanent request-validation failure that cannot succeed on retry."""


class ManagedExecution:
    def managed_start(self, owner, binding_id, request_id, snapshots, expected_build_key,
                      devices, npu_count, command, env, timeout_seconds=1800,
                      priority=0, queue_seconds=1800):
        if not isinstance(command, str) or not command.strip() or len(command) > 200000:
            raise ValueError("a bounded nonempty shell command is required")
        if not isinstance(env, dict) or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                                           or key.startswith("VAWS_REMOTE_JOB_") or key == "ASCEND_RT_VISIBLE_DEVICES"
                                           or not isinstance(value, str) for key, value in env.items()):
            raise ValueError("invalid environment or attempted override of managed device ownership")
        if not 1 <= timeout_seconds <= 86400:
            raise ValueError("timeout_seconds must be 1..86400")
        key = digest([owner, binding_id, request_id])
        request = {"binding_id": binding_id, "request_id": request_id, "snapshots": snapshots,
                   "expected_build_key": expected_build_key, "devices": devices, "npu_count": npu_count,
                   "priority": priority, "queue_seconds": queue_seconds}
        specification = {"command": command, "env": env, "timeout_seconds": timeout_seconds}
        with self.lock, self.transaction() as db:
            binding = self.owned(db, "binding", binding_id, owner)
            existing = [row for row in self.rows(db, "job") if row["id"] == key]
            if existing:
                job = existing[0]
                if job["request"] != request or job["spec"] != specification:
                    raise ValueError("managed execution request id reused with different arguments")
                return job
            if binding["state"] != "bound":
                raise ValueError("cannot start a managed job on a returned runtime")
            job = {"id": key, "owner": owner, "binding_id": binding_id,
                   "session": binding["intent"]["session"], "request": request, "spec": specification,
                   "job_id": "vaws-" + key, "state": "pending", "last_poll": 0,
                   "cancel_requested": False, "force": False}
            self.put(db, "job", job)  # intent survives a crash before lease creation
            self.event(db, owner, "managed-job-created", run=key, job_id=job["job_id"])
        return self.managed_advance(key)

    def managed_control(self, owner, job_id, action="status", force=False):
        with self.lock, self.transaction() as db:
            job = self.owned(db, "job", job_id, owner)
            binding = self.owned(db, "binding", job["binding_id"], owner)
            runtime = self.get(db, "runtime", binding["runtime_id"])
            if action == "stop":
                job.update(cancel_requested=True, force=bool(job.get("force") or force))
                self.put(db, "job", job)
            elif action not in {"status", "tail"}:
                raise ValueError("managed execution action must be status, tail or stop")
        if action == "tail":
            return {**job, "remote": self.backend.job(runtime, job["job_id"], "tail")}
        return self.managed_advance(job_id) if job["state"] not in JOB_TERMINAL else job

    def managed_advance(self, key):
        # Per-job lock: remote probes/supervision for one job never block
        # another job's advancement; the global lock guards only DB sections.
        with self._entity_lock("job", key):
            with self.lock, self.transaction() as db:
                job = self.get(db, "job", key)
                binding = self.owned(db, "binding", job["binding_id"], job["owner"])
                runtime = self.get(db, "runtime", binding["runtime_id"])
                runs = [row for row in self.rows(db, "run") if row["id"] == key]
            if job["state"] in JOB_TERMINAL:
                return job
            try:
                if not runs:
                    if job["cancel_requested"]:
                        self.return_runtime(job["owner"], binding["id"])
                        job["state"] = "cancelled"
                        job["runtime_returned"] = True
                        return self._save_managed(job)
                    run = self.request_run(job["owner"], **job["request"])
                else:
                    run = self.control(job["owner"], key, "poll", _managed=True)
                job["lease_state"] = run["state"]
                observed = self.backend.job(runtime, job["job_id"], "status")
                job["had_receipt"] = bool(job.get("had_receipt") or observed.get("receipt")
                                          or (job.get("remote") or {}).get("receipt"))
                if observed.get("state") == "absent" and job["had_receipt"]:
                    # A job directory that existed and then disappeared is lost
                    # supervision, not a drained family. It must never satisfy
                    # completion_confirmed or disable retain_until_release.
                    observed = {**observed, "quiet": False}
                job["remote"] = observed
                if run["state"] in {"uncertain", "pending"}:
                    job.update(state="waiting" if run["state"] == "pending" else "uncertain", error=run.get("error"))
                    return self._save_managed(job)

                timed_out = (observed.get("result") or {}).get("state") == "timeout"
                if timed_out:
                    job["timed_out"] = True
                if run["state"] == "orphaned_busy" and not job["cancel_requested"] and not timed_out:
                    # Host quarantine is not proof of a dead family. Heartbeat
                    # is the only recovery that moves orphaned_busy back to
                    # active; a live marked family is never stopped here.
                    run = self.control(job["owner"], key, "heartbeat", _managed=True)
                    job["lease_state"] = run["state"]
                    if run["state"] == "orphaned_busy":
                        job.update(state="uncertain",
                                   error="host quarantines the lease; the live family is preserved")
                        return self._save_managed(job)
                lost_lease = run["state"] in LEASE_TERMINAL
                if job["cancel_requested"] or lost_lease or timed_out:
                    if not observed["quiet"]:
                        job["remote"] = self.backend.job(runtime, job["job_id"], "stop", force=job["force"])
                        job["state"] = "stopping"
                        return self._save_managed(job)
                    return self._finish_managed(job, run, binding, runtime, observed)

                if observed["state"] not in {"absent", "prepared", "running"}:
                    if observed["quiet"]:
                        return self._finish_managed(job, run, binding, runtime, observed)
                    job.update(state="uncertain", error="remote process ownership is unresolved")
                    return self._save_managed(job)

                if run["state"] == "queued":
                    job["state"] = "queued"
                    return self._save_managed(job)
                if run["state"] == "granted":
                    run = self.control(job["owner"], key, "preflight", _managed=True)
                if run["state"] == "starting":
                    command = (binding.get("launch_preamble", "") + "\nexport ASCEND_RT_VISIBLE_DEVICES="
                               + shlex.quote(run["environment"]["ASCEND_RT_VISIBLE_DEVICES"])
                               + "\n" + job["spec"]["command"])
                    specification = {**job["spec"], "command": command, "cwd": binding["endpoint"]["cwd"],
                                     "env": {**job["spec"]["env"], **run["environment"]}}
                    observed = self.backend.job(runtime, job["job_id"], "prepare", spec=specification)
                    job["remote"] = observed
                    if observed["state"] != "prepared":
                        raise RuntimeError("start gate has no verified waiting supervisor")
                    pid = self.backend.job_host_pid(runtime, observed["receipt"])
                    run = self.control(job["owner"], key, "activate", pid, _managed=True,
                                       process_guard=observed["receipt"]["process_guard"])
                if run["state"] == "active":
                    # This renewal belongs to a persisted job, not an idle AI
                    # connection. A manager restart reconciles the same job id.
                    run = self.control(job["owner"], key, "heartbeat", _managed=True)
                    if run["state"] != "active":
                        raise RuntimeError("lease is not active; the start gate remains closed")
                    if observed["state"] == "prepared":
                        authorization = {"run_id": key, "epoch": run["epoch"], "fence": run["task"]["fence_token"]}
                        observed = self.backend.job(runtime, job["job_id"], "go", authorization=authorization)
                    elif observed["state"] == "absent":
                        raise RuntimeError("active lease lost its job receipt; do not relaunch")
                    job.update(state="running", remote=observed, lease_state=run["state"])
                else:
                    job.update(state=run["state"], lease_state=run["state"])
                job.pop("error", None)
            except ExecutionRequestError as exc:
                # Deterministic validation failures (bad snapshot, build_key
                # mismatch, an unresolved execution on the binding) can never
                # succeed on retry. Fail terminally instead of wedging the
                # binding in an uncertain poll loop.
                job.update(state="failed", error=str(exc)[:500])
            except Exception as exc:
                job.update(state="uncertain", error=str(exc)[:500])
            return self._save_managed(job)

    def _save_managed(self, job):
        job["last_poll"] = self.clock()
        with self.transaction() as db:
            # Stop intent is monotonic. A concurrent control request may have
            # persisted cancellation while this advancement was doing remote
            # work from an older snapshot; never overwrite it on save.
            current = self.get(db, "job", job["id"])
            if current.get("cancel_requested"):
                job["cancel_requested"] = True
                job["force"] = bool(job.get("force") or current.get("force"))
            self.put(db, "job", job)
            runs = [row for row in self.rows(db, "run") if row["id"] == job["id"]]
            binding = self.get(db, "binding", job["binding_id"])
        if runs:
            self.export_manifest(runs[0], binding, job=job)
        return job

    def _finish_managed(self, job, run, binding, runtime, observed):
        # The remote subreaper reports quiet only after all descendants drain.
        # Host GC cannot substitute an empty marker scan for this observation.
        if not observed["quiet"]:
            raise RuntimeError("cannot release a job without confirmed process completion")
        if run["state"] not in LEASE_TERMINAL:
            action = "cancel" if run["state"] in {"pending", "queued", "granted"} else "release"
            run = self.control(job["owner"], job["id"], action, _managed=True, completion_confirmed=True)
        job["lease_state"] = run["state"]
        if run["state"] not in LEASE_TERMINAL:
            job.update(state="releasing", error=run.get("error"))
            return self._save_managed(job)
        self.return_runtime(job["owner"], binding["id"])
        # Re-verify the returned runtime before another client can receive it.
        # Cleaning consists only of the owned process family; code/records stay.
        # This automatic re-registration is verification-equivalent to the
        # administrator path named by runtime_return: register() re-runs the
        # same idle-container inspection plus full profile/source verification,
        # and any failure leaves the runtime quarantined in needs_repair.
        try:
            if not runtime.get("draining"):
                self.register(runtime["id"], {key: runtime[key] for key in ("host_endpoint", "endpoint", "container_name", "service_ports")})
        except Exception as exc:
            job["runtime_reuse_error"] = str(exc)[:500]
        state = ("cancelled" if job["cancel_requested"] else "timeout" if job.get("timed_out")
                 else observed.get("state", "lost_outcome"))
        job["state"] = state if state in JOB_TERMINAL else "inconclusive"
        job["remote"] = observed
        job["runtime_returned"] = True
        job.pop("error", None)
        return self._save_managed(job)

    def managed_tick(self, limit=4):
        with self.transaction() as db:
            jobs = [row for row in self.rows(db, "job") if row["state"] not in JOB_TERMINAL]
        for row in sorted(jobs, key=lambda item: item["last_poll"])[:limit]:
            self.managed_advance(row["id"])
