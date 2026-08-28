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
        result = handle_request({**request, "state_dir": str(self.state), "interval_seconds": 0.001},
                                probe=lambda: {"status": "ok", "devices": [0, 1],
                                               "busy": {str(d): ["test worker"] for d in self.busy}})
        if self.fail_after == request["action"]:
            self.fail_after = None
            raise TimeoutError("reply lost after host mutation")
        return result


def runtime_spec(number):
    return {"endpoint": {"host": "192.0.2.1", "port": 46000 + number, "root": "/vllm-workspace"},
            "host_endpoint": {"host": "192.0.2.1", "port": 22}, "container_name": "prepared-" + str(number)}


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


class ProfileTests(unittest.TestCase):
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
                (root / "kernels.so").unlink()
                with self.assertRaisesRegex(ValueError, "missing required"):
                    verify(root, manifest)
                restore(root, bundle, manifest["build_key"])
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


if __name__ == "__main__":
    unittest.main()
