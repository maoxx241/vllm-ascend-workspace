from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / ".agents/lib"), str(ROOT / ".agents/coordinator")]
from vaws_npu_coordination import handle_request, _confirmed_free_probe, CoordinationError
from vaws_ready_runtime import RuntimePool
from vaws_runtime_profile import capture, digest, verify, publish, restore


class Backend:
    """Real SQLite host protocol, simulated occupancy and prepared containers."""
    def __init__(self, state):
        self.state = state
        self.busy = []
        self.fail = False
        self.fail_after = None
        self.calls = []
        self.jobs = {}
        self.fail_job_after = None
        self.attestation = {"profile_key": "profile-a", "build_key": "native-a",
                            "profile": {"launch_env": {"VLLM_VERSION": "test"}}}

    def inspect(self, runtime, **kwargs):
        if self.fail:
            raise TimeoutError("probe unavailable")
        self.calls.append(("inspect", kwargs))
        return copy.deepcopy(self.attestation)

    def host(self, runtime, request):
        if self.fail:
            raise TimeoutError("host unavailable")
        self.calls.append(("host", request["action"]))
        def guarded(value, *, completion_confirmed=False):
            guard = json.loads(value) if isinstance(value, str) else value
            return bool(guard) and (any(not job["quiet"] and job["receipt"]["process_guard"] == guard for job in self.jobs.values())
                                   or (guard.get("retain_until_release") and not completion_confirmed))
        with mock.patch("vaws_npu_coordination.process_guard_busy", side_effect=guarded):
            result = handle_request({**request, "state_dir": str(self.state), "interval_seconds": 0.001},
                                    probe=lambda: {"status": "ok", "devices": [0, 1],
                                                   "busy": {str(d): ["test worker"] for d in self.busy}})
        if self.fail_after == request["action"]:
            self.fail_after = None
            raise TimeoutError("reply lost after host mutation")
        return result

    def job(self, runtime, job_id, action, **params):
        self.calls.append(("job", action))
        if action == "prepare" and job_id not in self.jobs:
            self.jobs[job_id] = {"state": "prepared", "quiet": False,
                                 "receipt": {"pid": 4242, "process_guard": {"marker": job_id[-32:], "boot_id": "test-boot",
                                                                          "retain_until_release": True}}}
        if action == "go":
            self.jobs[job_id]["state"] = "running"
        if action == "stop" and job_id in self.jobs:
            self.jobs[job_id].update(state="cancelled", quiet=True)
        if self.fail_job_after == action:
            self.fail_job_after = None
            raise TimeoutError("lost job reply")
        return copy.deepcopy(self.jobs.get(job_id, {"state": "absent", "quiet": True}))

    def job_host_pid(self, runtime, receipt):
        return receipt["pid"]


def runtime_spec(number):
    return {"endpoint": {"host": "192.0.2.1", "port": 46000 + number, "root": "/vllm-workspace"},
            "host_endpoint": {"host": "192.0.2.1", "port": 22}, "container_name": "prepared-" + str(number), "service_ports": [48000 + number]}


class BackendTests(unittest.TestCase):
    def test_docker_idle_probe_keeps_pid_column_and_rejects_workers(self):
        from backend import RemoteBackend

        backend = RemoteBackend()
        for rows, idle in [("PID STAT COMMAND\n123 S sleep\n", True),
                           ("PID STAT COMMAND\n123 Z python\n124 S sleep\n", True),
                           ("PID STAT COMMAND\n123 S python\n", False),
                           ("PID STAT COMMAND\n", False),
                           ("PID STAT COMMAND\nbroken\n", False)]:
            commands = []

            def bash(target, command):
                commands.append(command)
                if command.startswith("docker inspect"):
                    return json.dumps({"Id": "container-1", "State": {"Running": True}})
                if command.startswith("docker top"):
                    self.assertTrue(command.endswith("-eo pid,stat,comm"))
                    return rows
                if command.startswith("ss "):
                    return ""
                return json.dumps({"profile": {"launch_env": {}}})

            with self.subTest(rows=rows), mock.patch.object(backend, "bash", side_effect=bash), mock.patch("backend.launch_preamble", return_value=""):
                if idle:
                    self.assertEqual(backend.inspect(runtime_spec(1), idle=True)["container_id"], "container-1")
                else:
                    with self.assertRaisesRegex(RuntimeError, "not an idle"):
                        backend.inspect(runtime_spec(1), idle=True)
                    self.assertEqual(len(commands), 2)

    def test_bash_failure_carries_outcome_and_bounded_stderr_without_command(self):
        from backend import RemoteBackend

        backend = RemoteBackend()
        target = {"host": "192.0.2.1", "port": 22, "user": "root"}
        with tempfile.TemporaryDirectory() as tmp:
            stdout = Path(tmp) / "stdout.log"
            stderr = Path(tmp) / "stderr.log"
            stdout.write_text("")
            stderr.write_text("padding line\n" * 100 + "final: device probe permission denied")
            result = {"outcome": "failed", "status": "nonzero_exit", "exit_code": 17,
                      "refs": {"stdout": str(stdout), "stderr": str(stderr)}}
            with mock.patch("backend.resolve_endpoint", side_effect=lambda value: value), \
                    mock.patch("backend.remote_bash", return_value={"result": result}):
                with self.assertRaises(RuntimeError) as caught:
                    backend.bash(target, "echo SECRET-TOKEN-VALUE")
            message = str(caught.exception)
            self.assertIn("failed/nonzero_exit", message)
            self.assertIn("exit 17", message)
            self.assertIn("permission denied", message)
            self.assertNotIn("SECRET-TOKEN-VALUE", message)
            self.assertLessEqual(len(message), 400)  # bounded tail only
            blocked = {"outcome": "blocked", "status": "cwd_outside_root"}
            with mock.patch("backend.resolve_endpoint", side_effect=lambda value: value), \
                    mock.patch("backend.remote_bash", return_value={"result": blocked}):
                with self.assertRaisesRegex(RuntimeError, "blocked/cwd_outside_root"):
                    backend.bash(target, "true")


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backend = Backend(self.root / "host")
        self.pool = RuntimePool(self.root / "manager", self.backend)
        self.pool.register("runtime-a", runtime_spec(1))
        self.pool.register("runtime-b", runtime_spec(2))

    def tearDown(self):
        self.temp.cleanup()

    def bind(self, owner, root):
        session = self.pool.session_open(owner, "same-session-name", {"vllm": str(root / "vllm"), "vllm-ascend": str(root / "va")})
        return self.pool.checkout(owner, session["id"], "profile-a", "checkout")

    def request(self, owner, binding, **extra):
        return self.pool.request_run(owner, binding["id"], "request", {"vllm": "a" * 40, "vllm-ascend": "b" * 40}, "native-a", [0], 0, **extra)

    def test_two_management_roots_share_one_runtime_and_card_authority(self):
        with ThreadPoolExecutor(2) as workers:
            a, b = list(workers.map(lambda args: self.bind(*args), [("alice", self.root / "clone-a"), ("bob", self.root / "linked-b")]))
        self.assertNotEqual(a["runtime_id"], b["runtime_id"])
        arun, brun = self.request("alice", a), self.request("bob", b)
        self.assertEqual(arun["state"], "granted")
        self.assertEqual(brun["state"], "queued")
        self.assertEqual(self.pool.control("alice", arun["id"], "release")["state"], "released")
        restarted = RuntimePool(self.root / "manager", self.backend)
        restarted.tick()
        self.assertEqual(restarted.status("bob")["runs"][0]["state"], "granted")
        self.assertEqual(set(kind for kind, _ in self.backend.calls), {"inspect", "host"})

    def test_checkout_idempotency_authorization_and_no_silent_provision(self):
        binding = self.bind("alice", self.root / "a")
        self.assertEqual(self.bind("alice", self.root / "a")["id"], binding["id"])
        with self.assertRaises(PermissionError):
            self.pool.return_runtime("bob", binding["id"])
        with self.assertRaises(ValueError):
            self.pool.register("duplicate", runtime_spec(1))
        session = self.pool.session_open("bob", "other", {"va": str(self.root / "b")})
        miss = self.pool.checkout("bob", session["id"], "missing", "missing")
        self.assertEqual(miss["status"], "cache_miss")
        self.assertFalse(miss["provisioning_started"])
        spec = runtime_spec(3)
        spec["service_ports"] = [48001]
        with self.assertRaisesRegex(ValueError, "ports overlap"):
            self.pool.register("port-conflict", spec)

    def test_yield_acceptance_and_timeout_never_release_or_reassign(self):
        binding = self.bind("alice", self.root / "a")
        run = self.request("alice", binding)
        event = self.pool.message("bob", run["id"], "Could you release card 0 after this run?")
        self.pool.reply("alice", event["cursor"], "Yes, after completion")
        self.assertEqual(self.pool.status("alice")["runs"][0]["state"], "granted")
        self.assertEqual(len(self.pool.events("bob")["events"]), 1)
        with self.assertRaises(PermissionError):
            self.pool.reply("bob", event["cursor"], "unauthorized")
        self.backend.fail = True
        self.assertEqual(self.pool.control("alice", run["id"], "release")["state"], "uncertain")
        with self.assertRaises(ValueError):
            self.pool.return_runtime("alice", binding["id"])
        self.backend.fail = False
        self.backend.busy = [0]
        self.pool.control("alice", run["id"], "poll")
        self.assertEqual(self.pool.control("alice", run["id"], "release")["state"], "orphaned_busy")

    def test_lost_grant_reply_reconciles_same_task_after_restart(self):
        binding = self.bind("alice", self.root / "a")
        self.backend.fail_after = "acquire"
        run = self.request("alice", binding)
        self.assertEqual(run["state"], "uncertain")
        pool = RuntimePool(self.root / "manager", self.backend)
        recovered = pool.control("alice", run["id"], "poll")
        self.assertEqual(recovered["state"], "granted")
        self.assertEqual(recovered["task_id"], run["task_id"])

    def test_lost_submit_reply_and_initial_connection_failure_recover(self):
        binding = self.bind("alice", self.root / "a")
        self.backend.fail_after = "submit"
        run = self.request("alice", binding)
        self.assertEqual(run["state"], "uncertain")
        recovered = self.pool.control("alice", run["id"], "poll")
        self.assertEqual(recovered["state"], "granted")
        self.assertEqual(recovered["task_id"], run["task_id"])
        from vaws_run_manifest import load_manifest
        manifest = load_manifest(self.root / "manager/runs" / (run["id"] + ".json"))
        self.assertEqual(manifest["status"], "planned")
        self.assertEqual(manifest["environment"]["coordination"]["state"], "granted")
        other = self.bind("bob", self.root / "b")
        self.backend.fail_after = "status"
        pending = self.request("bob", other)
        self.assertEqual(pending["state"], "pending")
        self.assertIsNone(pending["epoch"])
        recovered = self.pool.control("bob", pending["id"], "poll")
        self.assertEqual(recovered["state"], "queued")

    def test_unsubmitted_request_expires_without_allocating_after_outage(self):
        import time
        binding = self.bind("alice", self.root / "a")
        self.backend.fail_after = "status"
        pending = self.request("alice", binding, queue_seconds=1)
        self.pool.clock = lambda: time.time() + 60
        expired = self.pool.control("alice", pending["id"], "poll")
        self.assertEqual(expired["state"], "expired")
        self.assertNotIn(("host", "submit"), self.backend.calls)

    def test_unsubmitted_pending_run_rejects_non_poll_actions_without_host_mutation(self):
        binding = self.bind("alice", self.root / "a")
        self.backend.fail_after = "status"
        pending = self.request("alice", binding)
        self.assertEqual(pending["state"], "pending")
        self.assertIsNone(pending["epoch"])

        calls_before = list(self.backend.calls)
        for action in ("preflight", "activate", "heartbeat", "release"):
            with self.subTest(action=action), self.assertRaisesRegex(
                ValueError, "unsubmitted pending execution; poll it first"
            ):
                self.pool.control("alice", pending["id"], action)
            self.assertEqual(self.backend.calls, calls_before)
            observed = self.pool.status("alice")["runs"][0]
            self.assertEqual(observed["state"], "pending")
            self.assertFalse(observed["submitted"])

    def test_unsubmitted_pending_run_can_cancel_without_submit(self):
        binding = self.bind("alice", self.root / "a")
        self.backend.fail_after = "status"
        pending = self.request("alice", binding)
        calls_before = len(self.backend.calls)

        cancelled = self.pool.control("alice", pending["id"], "cancel")

        self.assertEqual(cancelled["state"], "cancelled")
        self.assertNotIn(("host", "submit"), self.backend.calls[calls_before:])

    def test_native_refresh_is_forbidden_while_execution_is_unresolved(self):
        binding = self.bind("alice", self.root / "a")
        run = self.request("alice", binding)
        with self.assertRaises(ValueError):
            self.pool.refresh("alice", binding["id"])
        self.pool.control("alice", run["id"], "release")
        self.backend.attestation["build_key"] = "native-new"
        self.assertEqual(self.pool.refresh("alice", binding["id"])["build_key"], "native-new")

    def test_epoch_change_fails_closed_and_host_enforces_fence_epoch(self):
        binding = self.bind("alice", self.root / "a")
        run = self.request("alice", binding)
        import sqlite3
        with sqlite3.connect(self.backend.state / "coordinator.sqlite3") as db:
            db.execute("UPDATE meta SET value='new-epoch' WHERE key='coordination_epoch'")
        result = self.pool.control("alice", run["id"], "release")
        self.assertEqual(result["state"], "uncertain")
        with self.assertRaises(CoordinationError):
            self.backend.host(runtime_spec(1), {"action": "cancel", "task_id": run["task_id"], "coordination_epoch": run["epoch"]})

    def test_return_requires_fresh_admin_verification(self):
        binding = self.bind("alice", self.root / "a")
        self.assertEqual(self.pool.return_runtime("alice", binding["id"])["runtime_state"], "needs_repair")
        session = self.pool.session_open("bob", "bob-session", {"va": str(self.root / "b")})
        self.assertEqual(self.pool.checkout("bob", session["id"], "profile-a", "bob", binding["runtime_id"])["status"], "cache_miss")
        self.pool.register(binding["runtime_id"], runtime_spec(1))
        self.assertEqual(self.pool.checkout("bob", session["id"], "profile-a", "bob", binding["runtime_id"])["state"], "bound")

    def test_repeated_free_probe_requires_visibility_in_every_sample(self):
        samples = iter([{"status": "ok", "devices": [1], "busy": {}}, {"status": "ok", "devices": [0, 1], "busy": {}}])
        result = _confirmed_free_probe(samples=2, interval_seconds=0, probe=lambda: next(samples))
        self.assertEqual(result["free"], [1])

    def managed(self, owner, binding, device=0, request_id="managed"):
        return self.pool.managed_start(owner, binding["id"], request_id,
                                       {"vllm": "a" * 40, "vllm-ascend": "b" * 40},
                                       "native-a", [device], 0, "exec python task.py", {}, 60)

    def test_managed_gate_renew_restart_and_stop_one_preserves_peer(self):
        a, b = self.bind("alice", self.root / "a"), self.bind("bob", self.root / "b")
        first, second = self.managed("alice", a), self.managed("bob", b, 1)
        self.assertEqual((first["state"], second["state"]), ("running", "running"))
        self.assertLess(self.backend.calls.index(("host", "activate")), self.backend.calls.index(("job", "go")))
        with self.assertRaisesRegex(ValueError, "managed execution owns"):
            self.pool.control("alice", first["id"], "release")
        self.pool = RuntimePool(self.root / "manager", self.backend)
        self.pool.tick()
        self.assertIn(("host", "heartbeat"), self.backend.calls)
        self.pool.managed_control("alice", first["id"], "stop")
        ended = self.pool.managed_control("alice", first["id"])
        self.assertEqual(ended["state"], "cancelled")
        self.assertTrue(ended["runtime_returned"])
        self.assertEqual(self.pool.status("bob")["jobs"][0]["state"], "running")
        self.assertFalse(self.backend.jobs[second["job_id"]]["quiet"])
        self.assertEqual(next(row for row in self.pool.catalog() if row["runtime_id"] == a["runtime_id"])["state"], "ready")

    def test_lost_go_reply_reuses_the_same_job_and_does_not_prepare_again(self):
        binding = self.bind("alice", self.root / "a")
        self.backend.fail_job_after = "go"
        job = self.managed("alice", binding)
        self.assertEqual(job["state"], "uncertain")
        restarted = RuntimePool(self.root / "manager", self.backend)
        recovered = restarted.managed_control("alice", job["id"])
        self.assertEqual(recovered["state"], "running")
        self.assertEqual(self.backend.calls.count(("job", "prepare")), 1)
        self.assertEqual(self.backend.calls.count(("job", "go")), 1)
        with self.assertRaises(PermissionError):
            restarted.managed_control("bob", job["id"], "stop")

    def test_managed_finish_defers_release_for_real_device_occupancy(self):
        binding = self.bind("alice", self.root / "a")
        job = self.managed("alice", binding)
        self.backend.jobs[job["job_id"]].update(state="succeeded", quiet=True, result={"state": "succeeded", "exit_code": 0})
        self.backend.busy = [0]
        result = self.pool.managed_control("alice", job["id"])
        self.assertEqual((result["state"], result["lease_state"]), ("releasing", "orphaned_busy"))
        self.backend.busy = []
        result = self.pool.managed_control("alice", job["id"])
        self.assertEqual(result["state"], "succeeded")
        manifest = json.loads((self.root / "manager/runs" / (job["id"] + ".json")).read_text())
        self.assertEqual(manifest["status"], "inconclusive")
        self.assertEqual(manifest["environment"]["managed_execution"]["result"]["exit_code"], 0)

    def test_orphaned_busy_recovers_via_heartbeat_without_stopping_live_family(self):
        binding = self.bind("alice", self.root / "a")
        job = self.managed("alice", binding)
        self.assertEqual(job["state"], "running")
        import sqlite3
        with sqlite3.connect(self.backend.state / "coordinator.sqlite3") as db:
            db.execute("UPDATE tasks SET state='orphaned_busy'")
        self.backend.calls.clear()
        recovered = self.pool.managed_control("alice", job["id"])
        self.assertEqual((recovered["state"], recovered["lease_state"]), ("running", "active"))
        self.assertIn(("host", "heartbeat"), self.backend.calls)
        self.assertNotIn(("job", "stop"), self.backend.calls)
        self.assertFalse(self.backend.jobs[job["job_id"]]["quiet"])

    def test_disappeared_job_directory_never_confirms_completion(self):
        binding = self.bind("alice", self.root / "a")
        job = self.managed("alice", binding)
        self.backend.jobs.clear()  # the whole job directory vanished remotely
        self.pool.managed_control("alice", job["id"], "stop")
        result = self.pool.managed_control("alice", job["id"])
        self.assertEqual(result["state"], "stopping")
        self.assertNotIn(("host", "release"), self.backend.calls)

    def test_deterministic_validation_failure_fails_terminally_without_retry(self):
        binding = self.bind("alice", self.root / "a")
        job = self.pool.managed_start("alice", binding["id"], "managed",
                                      {"vllm": "a" * 40, "vllm-ascend": "b" * 40},
                                      "wrong-build-key", [0], 0, "exec python task.py", {}, 60)
        self.assertEqual(job["state"], "failed")
        self.assertIn("cache miss", job["error"])
        calls = list(self.backend.calls)
        self.pool.managed_tick()
        self.assertEqual(self.backend.calls, calls)

    def test_operator_reconcile_unwedges_an_uncertain_run_and_records_event(self):
        binding = self.bind("alice", self.root / "a")
        job = self.managed("alice", binding)
        import sqlite3
        with sqlite3.connect(self.backend.state / "coordinator.sqlite3") as db:
            db.execute("UPDATE meta SET value='new-epoch' WHERE key='coordination_epoch'")
        wedged = self.pool.managed_control("alice", job["id"])
        self.assertEqual(wedged["state"], "uncertain")
        with self.assertRaises(ValueError):
            self.pool.reconcile("admin", job["id"], "")
        result = self.pool.reconcile("admin", job["id"], "host rebooted; epoch and tasks verified gone")
        self.assertEqual(result["run"]["state"], "cancelled")
        self.assertEqual(result["jobs"][0]["state"], "inconclusive")
        self.assertEqual(self.pool.events("admin")["events"][-1]["kind"], "run-reconciled")
        with self.assertRaises(ValueError):
            self.pool.reconcile("admin", job["id"], "already terminal")
        self.assertEqual(self.pool.return_runtime("alice", binding["id"])["status"], "returned")

    def test_task_worktrees_can_change_only_between_returned_bindings(self):
        binding = self.bind("alice", self.root / "a")
        with self.assertRaisesRegex(ValueError, "return task runtimes"):
            self.pool.session_open("alice", "same-session-name", {"va": "/new/worktree"})
        self.pool.return_runtime("alice", binding["id"])
        result = self.pool.session_open("alice", "same-session-name", {"va": "/new/worktree"})
        self.assertEqual(result["id"], binding["intent"]["session"])

    def test_drain_waits_for_existing_managed_job_and_disables_automatic_reuse(self):
        binding = self.bind("alice", self.root / "a")
        job = self.managed("alice", binding)
        self.assertEqual(self.pool.drain(binding["runtime_id"])["state"], "bound")
        self.assertEqual(self.pool.managed_control("alice", job["id"])["state"], "running")
        self.pool.managed_control("alice", job["id"], "stop")
        self.pool.managed_control("alice", job["id"])
        runtime = next(row for row in self.pool.catalog() if row["runtime_id"] == binding["runtime_id"])
        self.assertEqual(runtime["state"], "draining")
        self.pool.register(binding["runtime_id"], runtime_spec(1))
        self.assertFalse(next(row for row in self.pool.catalog() if row["runtime_id"] == binding["runtime_id"])["draining"])

    def test_a_hung_host_probe_blocks_only_its_own_runtime(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        original = self.backend.inspect

        def inspect(runtime, **kwargs):
            if runtime.get("container_name") == "prepared-1":
                entered.set()
                release.wait(10)
            return original(runtime, **kwargs)

        self.backend.inspect = inspect
        try:
            alice = self.pool.session_open("alice", "hang-a", {"va": str(self.root / "ha")})
            bob = self.pool.session_open("bob", "hang-b", {"va": str(self.root / "hb")})
            with ThreadPoolExecutor(2) as workers:
                stuck = workers.submit(self.pool.checkout, "alice", alice["id"], "profile-a", "hang-a", "runtime-a")
                self.assertTrue(entered.wait(10))
                other = workers.submit(self.pool.checkout, "bob", bob["id"], "profile-a", "hang-b", "runtime-b")
                self.assertEqual(other.result(timeout=10)["state"], "bound")
                release.set()
                self.assertEqual(stuck.result(timeout=10)["state"], "bound")
        finally:
            release.set()

    def test_concurrent_checkout_races_never_double_bind_a_runtime(self):
        pool = RuntimePool(self.root / "single", self.backend)
        pool.register("runtime-c", runtime_spec(3))
        session = pool.session_open("alice", "race-session", {"va": str(self.root / "r")})
        with ThreadPoolExecutor(4) as workers:
            results = list(workers.map(lambda index: pool.checkout("alice", session["id"], "profile-a", "race-" + str(index)), range(4)))
        bound = [row for row in results if row.get("state") == "bound"]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["runtime_id"], "runtime-c")
        self.assertTrue(all(row.get("status") == "cache_miss" for row in results if row is not bound[0]))
        self.assertEqual(next(row for row in pool.catalog() if row["runtime_id"] == "runtime-c")["state"], "bound")
        with ThreadPoolExecutor(2) as workers:
            replays = list(workers.map(lambda _: pool.checkout("alice", session["id"], "profile-a", "race-0"), range(2)))
        self.assertEqual({row["id"] for row in replays}, {bound[0]["id"]})

    def test_concurrent_conflicting_registration_has_exactly_one_winner(self):
        def attempt(index):
            try:
                return self.pool.register("duplicate-" + str(index), runtime_spec(9))
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(2) as workers:
            results = list(workers.map(attempt, range(2)))
        self.assertEqual(sum(isinstance(row, dict) for row in results), 1)
        self.assertTrue(any(isinstance(row, str) and "already registered" in row for row in results))

    def test_never_submitted_pending_run_cancels_and_expires_while_host_stays_down(self):
        import time
        binding = self.bind("alice", self.root / "a")
        self.backend.fail_after = "status"
        pending = self.request("alice", binding)
        self.assertEqual((pending["state"], pending["epoch"]), ("pending", None))
        self.backend.fail = True  # the host never comes back
        calls = list(self.backend.calls)
        cancelled = self.pool.control("alice", pending["id"], "cancel")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.backend.calls, calls)  # no host contact at all

        self.backend.fail = False
        self.backend.fail_after = "status"
        second = self.pool.request_run("alice", binding["id"], "request-2",
                                       {"vllm": "a" * 40, "vllm-ascend": "b" * 40}, "native-a", [0], 0, queue_seconds=1)
        self.assertEqual(second["state"], "pending")
        self.backend.fail = True
        self.pool.clock = lambda: time.time() + 60
        calls = list(self.backend.calls)
        expired = self.pool.control("alice", second["id"], "poll")
        self.assertEqual(expired["state"], "expired")
        self.assertEqual(self.backend.calls, calls)
        self.assertNotIn(("host", "submit"), self.backend.calls)

    def test_checkout_replay_of_a_returned_binding_fails_explicitly(self):
        binding = self.bind("alice", self.root / "a")
        self.assertEqual(self.bind("alice", self.root / "a")["id"], binding["id"])
        self.pool.return_runtime("alice", binding["id"])
        with self.assertRaisesRegex(ValueError, "returned binding"):
            self.bind("alice", self.root / "a")
        fresh = self.pool.checkout("alice", binding["intent"]["session"], "profile-a", "checkout-2")
        self.assertEqual(fresh["state"], "bound")
        self.assertNotEqual(fresh["id"], binding["id"])
        # The returned runtime stays quarantined; the fresh request binds the
        # other ready runtime instead of replaying the dead binding.
        self.assertNotEqual(fresh["runtime_id"], binding["runtime_id"])
        quarantined = next(row for row in self.pool.catalog() if row["runtime_id"] == binding["runtime_id"])
        self.assertEqual(quarantined["state"], "needs_repair")

    def test_transaction_closes_connections_and_rolls_back(self):
        import sqlite3
        with self.assertRaises(RuntimeError):
            with self.pool.transaction() as db:
                db.execute("INSERT INTO records VALUES('kind','ident','{}')")
                raise RuntimeError("boom")
        with self.pool.transaction() as db:
            self.assertIsNone(db.execute("SELECT data FROM records WHERE kind='kind'").fetchone())
            handle = db
        with self.assertRaises(sqlite3.ProgrammingError):
            handle.execute("SELECT 1")

    def test_checkout_marks_a_drifted_runtime_needs_repair_and_binds_the_next_candidate(self):
        original = self.backend.inspect

        def inspect(runtime, **kwargs):
            observed = original(runtime, **kwargs)
            if runtime.get("container_name") == "prepared-1":
                observed["build_key"] = "drifted"
            return observed

        self.backend.inspect = inspect
        binding = self.bind("alice", self.root / "a")
        self.assertEqual(binding["runtime_id"], "runtime-b")
        drifted = next(row for row in self.pool.catalog() if row["runtime_id"] == "runtime-a")
        self.assertEqual(drifted["state"], "needs_repair")
        self.assertIn("prepared environment changed", drifted["error"])
        session = self.pool.session_open("bob", "bob-session", {"va": str(self.root / "b")})
        self.assertEqual(self.pool.checkout("bob", session["id"], "profile-a", "bob", "runtime-a")["status"], "cache_miss")

    def test_reconcile_defends_identifiers_and_binding_ownership_consistency(self):
        binding = self.bind("alice", self.root / "a")
        run = self.request("alice", binding)
        with self.assertRaisesRegex(ValueError, "invalid identifier"):
            self.pool.reconcile("admin", "bad id!", "host inspected")
        with self.assertRaisesRegex(ValueError, "invalid identifier"):
            self.pool.reconcile("bad admin!", run["id"], "host inspected")
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.pool.reconcile("admin", run["id"], "host inspected", evidence="looked")
        with self.pool.transaction() as db:
            corrupted = self.pool.get(db, "binding", binding["id"])
            corrupted["owner"] = "bob"
            self.pool.put(db, "binding", corrupted)
        with self.assertRaises(PermissionError):
            self.pool.reconcile("admin", run["id"], "host inspected")

    def test_reconcile_terminates_a_vanished_job_wedge_only_with_evidence(self):
        binding = self.bind("alice", self.root / "a")
        job = self.managed("alice", binding)
        self.assertEqual(job["state"], "running")
        self.backend.jobs.clear()  # the whole job directory vanished remotely
        self.pool.managed_control("alice", job["id"], "stop")
        self.assertEqual(self.pool.managed_control("alice", job["id"])["state"], "stopping")
        reason = "job directory vanished while the lease stayed active"
        with self.assertRaisesRegex(ValueError, "evidence"):
            self.pool.reconcile("admin", job["id"], reason, force_release=True)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.pool.reconcile("admin", job["id"], reason)
        self.backend.calls.clear()
        result = self.pool.reconcile("admin", job["id"], reason,
                                     evidence="host ls shows no job directory; docker top shows no family",
                                     force_release=True)
        self.assertEqual(result["run"]["state"], "cancelled")
        self.assertEqual(result["jobs"][0]["state"], "inconclusive")
        self.assertEqual(self.backend.calls, [("host", "release")])
        self.assertTrue(result["event"]["force_release"])
        self.assertIn("no job directory", result["event"]["evidence"])
        self.assertEqual(self.pool.return_runtime("alice", binding["id"])["status"], "returned")


class ProfileTests(unittest.TestCase):
    def test_attestation_requires_populated_pinned_native_submodules(self):
        import subprocess
        from prepare_runtime import require_clean_sources

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(repo, *args):
                return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()

            def init(repo):
                repo.mkdir(parents=True)
                git(repo, 'init')
                git(repo, 'config', 'user.name', 'Test')
                git(repo, 'config', 'user.email', 'test@example.invalid')
                (repo / 'kernel.cpp').write_text('native input\n')
                git(repo, 'add', '.')
                git(repo, 'commit', '-m', 'base')

            for name in ('vllm', 'vllm-ascend'):
                init(root / name)
            va = root / 'vllm-ascend'
            (va / '.gitmodules').write_text('[submodule "catlass"]\n path = csrc/catlass\n url = ./catlass\n')
            (va / '.gitignore').write_text('csrc/catlass\n')
            git(va, 'add', '.')
            git(va, 'commit', '-m', 'declare native dependency')
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                require_clean_sources(root)
            child = va / 'csrc/catlass'
            init(child)
            with self.assertRaisesRegex(ValueError, 'tracked at its pinned commit'):
                require_clean_sources(root)
            git(va, 'update-index', '--add', '--cacheinfo', '160000,' + git(child, 'rev-parse', 'HEAD') + ',csrc/catlass')
            git(va, 'commit', '-m', 'pin dependency')
            require_clean_sources(root)
            (child / 'kernel.cpp').write_text('uncommitted native change\n')
            with self.assertRaisesRegex(ValueError, 'clean materialized'):
                require_clean_sources(root)

    def test_complete_bundle_hashes_missing_metadata_env_and_cache_reuse(self):
        from vaws_runtime_profile import PROFILE_FIELDS
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runtime"
            root.mkdir()
            for name in ["kernels.so", "binary_info_config.json", "cann.txt", "driver.txt", "smoke.txt"]:
                (root / name).write_text(name)
            profile = {key: "test-version" for key in PROFILE_FIELDS}
            profile.update(build_env={}, launch_env={"VLLM_VERSION": "test"}, compatibility_evidence="smoke.txt")
            profile["system_files"] = {name: {"path": str(root / (name + ".txt")), "sha256": hashlib.sha256((name + ".txt").encode()).hexdigest()} for name in ["cann", "driver"]}
            inputs = {"vllm": "native-a", "vllm-ascend": "native-b"}
            manifest = capture(root, profile, inputs, {"kernels.so": "library", "binary_info_config.json": "metadata"}, {"cann": "cann.txt", "driver": "driver.txt", "smoke": "smoke.txt"})
            verify(root, manifest, check_environment=False)
            with mock.patch("vaws_runtime_profile.importlib.metadata.version", return_value="test-version"), mock.patch("vaws_runtime_profile.sysconfig.get_config_var", return_value="test-version"):
                bundle = publish(root, Path(tmp) / "bundles", manifest)
                self.assertEqual(publish(root, Path(tmp) / "bundles", manifest), bundle)
                (root / "smoke.txt").write_text("same passed smoke, new timestamp")
                refreshed = copy.deepcopy(manifest)
                refreshed["evidence"]["smoke"]["sha256"] = hashlib.sha256((root / "smoke.txt").read_bytes()).hexdigest()
                self.assertEqual(publish(root, Path(tmp) / "bundles", refreshed), bundle)
                # Restoration also restores the original complete evidence,
                # rather than mixing its manifest with a newer smoke receipt.
                (root / "kernels.so").unlink()
                with self.assertRaisesRegex(ValueError, "missing required"):
                    verify(root, manifest)
                restore(root, bundle, manifest["build_key"])
                with mock.patch("vaws_runtime_profile.sysconfig.get_config_var", return_value="different-abi"):
                    with self.assertRaisesRegex(ValueError, "Python ABI changed"):
                        verify(root, manifest)
                (root / "binary_info_config.json").write_text("corrupt")
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    verify(root, manifest)
            with self.assertRaises(ValueError):
                capture(root, profile, inputs, {"kernels.so": "library"}, {})
            from vaws_runtime_profile import launch_preamble
            import os, subprocess
            profile["launch_env"]["PYTHONPATH"] = "/scoped/source"
            result = subprocess.check_output(["bash", "-c", launch_preamble(profile) + '\nprintf "%s" "$PYTHONPATH"'],
                                              text=True, env={**os.environ, "PYTHONPATH": "/base/acl:/base/native-compat"})
            self.assertEqual(result, "/scoped/source:/base/acl:/base/native-compat")

    def test_attest_records_smoke_timeout_as_evidence(self):
        import subprocess
        import prepare_runtime
        from vaws_runtime_profile import PROFILE_FIELDS

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = {key: "test-version" for key in PROFILE_FIELDS}
            profile.update(build_env={}, launch_env={}, compatibility_evidence="smoke-ref",
                           system_files={name: {"path": str(root / (name + ".txt")), "sha256": "0" * 64} for name in ("cann", "driver")})
            with mock.patch("prepare_runtime.require_clean_sources"), \
                    mock.patch("prepare_runtime.runtime_build_inputs", return_value={"vllm": "native-a"}), \
                    mock.patch("prepare_runtime.subprocess.run",
                               side_effect=subprocess.TimeoutExpired(cmd="smoke", timeout=60)):
                with self.assertRaisesRegex(ValueError, "timed out"):
                    prepare_runtime.attest(root, {"profile": profile, "files": {}})
            evidence = json.loads((root / ".vaws-runtime/profile-evidence/smoke.json").read_text())
            self.assertFalse(evidence["passed"])
            self.assertIn("timed out", evidence["error"])
            self.assertEqual(evidence["build_inputs"], {"vllm": "native-a"})


if __name__ == "__main__":
    unittest.main()
