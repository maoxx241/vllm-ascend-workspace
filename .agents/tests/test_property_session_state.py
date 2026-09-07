#!/usr/bin/env python3
"""Property tests for session identity, leases and locks.

Modules: ``.agents/lib/vaws_session_id.py`` and ``vaws_session_state.py``.

Properties:

* ``normalize_session_id`` is total, idempotent, deterministic, bounded and
  keeps distinct long inputs distinct;
* lease allocation agrees with a reference model over random operation
  sequences: no NPU device or port is ever owned by two sessions, releases
  only affect the caller's resources, and live leases equal the model;
* concurrent allocators (real threads, real file lock) never hand out the
  same device twice;
* the file lock excludes a live holder, recovers a crashed (stale) holder, and
  never reads a crashed holder's lock as "available" before the stale window.

The stale-lock recovery race is reproduced deterministically with event
ordering rather than timing and recorded as a known defect.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_session_state as state  # noqa: E402
from vaws_session_id import derive_from_branch, normalize_session_id  # noqa: E402
from vaws_session_state import SessionStateError, allocate_service_port, allocate_session_leases, file_lock, parse_port_range, release_all_session_leases, release_service_port, safe_token, session_container_name, session_live_leases  # noqa: E402
from vaws_validate import ValidationError  # noqa: E402
from test_property_support import MULTIBYTE, Gen, run_cases  # noqa: E402

ID_CHARS = "abcXYZ019._-/ \t:@#" + MULTIBYTE
VALID_SHAPE = r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$|^[a-z0-9]{3}$"


class NormalizeSessionIdProperties(unittest.TestCase):
    def test_output_is_none_or_a_bounded_canonical_id(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            raw = gen.one_of(
                lambda: gen.text(ID_CHARS, 0, 30),
                lambda: gen.text(ID_CHARS, 60, 200),
                lambda: gen.choice(("", "..", "---", "a", "ab", "abc", "A-B_C.d", "  x y z  ", "pr/12", "\u0130stanbul", "\u212aelvin")),
            )
            result = normalize_session_id(raw)
            if result is None:
                return
            self.assertTrue(3 <= len(result) <= 64, result)
            self.assertRegex(result, r"^[a-z0-9._-]+$")
            self.assertNotIn(result[0], ".-_")
            self.assertNotIn(result[-1], ".-_")
            self.assertNotIn("--", result)
            self.assertEqual(normalize_session_id(result), result, "normalization must be idempotent")
            self.assertEqual(normalize_session_id(raw), result, "normalization must be deterministic")
            self.assertTrue(result.isascii())

        run_cases(1500, body, label="normalize_session_id")

    def test_canonical_ids_are_fixed_points(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            middle = gen.text("abcxyz019._-", 1, 62).replace("--", "-x")
            value = gen.text("abcxyz019", 1, 1) + middle + gen.text("abcxyz019", 1, 1)
            self.assertEqual(normalize_session_id(value), value)

        run_cases(400, body, label="fixed points")

    def test_long_inputs_that_share_a_prefix_stay_distinct(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            prefix = gen.text("abcxyz019", 70, 90)
            a = normalize_session_id(prefix + gen.text("abc", 1, 4))
            b = normalize_session_id(prefix + gen.text("xyz", 1, 4))
            self.assertIsNotNone(a)
            self.assertIsNotNone(b)
            self.assertNotEqual(a, b)
            self.assertLessEqual(len(a or ""), 64)

        run_cases(200, body, label="long id distinctness")

    def test_branch_derivation_yields_only_canonical_ids(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            tail = gen.text(ID_CHARS, 0, 40)
            branch = gen.choice((f"session/{tail}", f"task/{tail}", f"pr/{tail}", f"feature/{tail}", tail, None))
            derived = derive_from_branch(branch)
            if derived is None:
                self.assertTrue(branch is None or not branch.startswith(("session/", "task/", "pr/")) or normalize_session_id(branch.split("/", 1)[1]) is None or (branch.startswith("pr/")))
                return
            self.assertEqual(normalize_session_id(derived), derived)
            if branch and branch.startswith("pr/"):
                self.assertTrue(derived.startswith("pr-"))

        run_cases(400, body, label="derive_from_branch")


class LeaseModel:
    """Reference model: resource -> owning session, per machine."""

    def __init__(self) -> None:
        self.devices: dict[int, str] = {}
        self.ssh_ports: dict[int, str] = {}
        self.service_ports: dict[int, str] = {}

    def live(self, sid: str) -> dict[str, list[int]]:
        return {
            "npu_devices": sorted(d for d, o in self.devices.items() if o == sid),
            "container_ssh_ports": sorted(p for p, o in self.ssh_ports.items() if o == sid),
            "service_ports": sorted(p for p, o in self.service_ports.items() if o == sid),
        }


class LeaseModelProperties(unittest.TestCase):
    SESSIONS = ("sess-a", "sess-b", "sess-c")
    AVAILABLE = [0, 1, 2, 3]
    SSH_RANGE = "46000:46003"
    SERVICE_RANGE = "30000:30003"

    def test_random_operation_sequences_agree_with_the_model(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                model = LeaseModel()
                for _step in range(gen.integer(3, 10)):
                    sid = gen.choice(self.SESSIONS)
                    op = gen.choice(("alloc-devices", "alloc-count", "alloc-service", "release-service", "release-all"))
                    if op == "alloc-devices":
                        requested = sorted(gen.sample(self.AVAILABLE, gen.integer(1, 3)))
                        conflict = [d for d in requested if model.devices.get(d, sid) != sid]
                        port_taken_by_other = [p for p in range(46000, 46004) if model.ssh_ports.get(p, sid) != sid]
                        free_port_exists = any(model.ssh_ports.get(p, sid) == sid for p in range(46000, 46004))
                        try:
                            result = allocate_session_leases(repo_root=repo, machine_alias="m", session_id=sid, requested_devices=requested, available_devices=self.AVAILABLE, container_ssh_port_range=self.SSH_RANGE, port_available=lambda _p: True)
                        except SessionStateError as exc:
                            self.assertTrue(conflict or not free_port_exists, f"allocation rejected without a modelled conflict: {exc}")
                            if conflict:
                                self.assertIn("already leased", str(exc))
                            continue
                        self.assertEqual(conflict, [], "allocation succeeded despite a device owned by another session")
                        self.assertEqual(result["npu_devices"], requested)
                        for d in requested:
                            model.devices[d] = sid
                        port = result["container_ssh_port"]
                        self.assertIn(port, range(46000, 46004))
                        self.assertNotIn(port, port_taken_by_other)
                        model.ssh_ports[port] = sid
                    elif op == "alloc-count":
                        count = gen.integer(1, 3)
                        free = [d for d in self.AVAILABLE if model.devices.get(d, sid) == sid]
                        free_port_exists = any(model.ssh_ports.get(p, sid) == sid for p in range(46000, 46004))
                        try:
                            result = allocate_session_leases(repo_root=repo, machine_alias="m", session_id=sid, npu_count=count, available_devices=self.AVAILABLE, container_ssh_port_range=self.SSH_RANGE, port_available=lambda _p: True)
                        except SessionStateError:
                            self.assertTrue(len(free) < count or not free_port_exists)
                            continue
                        self.assertGreaterEqual(len(free), count)
                        self.assertEqual(result["npu_devices"], free[:count])
                        for d in result["npu_devices"]:
                            model.devices[d] = sid
                        model.ssh_ports[result["container_ssh_port"]] = sid
                    elif op == "alloc-service":
                        requested_port = gen.choice((None, gen.integer(30000, 30003), 29999))
                        try:
                            port = allocate_service_port(repo_root=repo, machine_alias="m", session_id=sid, requested_port=requested_port, serving_port_range=self.SERVICE_RANGE, port_available=lambda _p: True)
                        except SessionStateError:
                            if requested_port is None:
                                self.assertFalse(any(model.service_ports.get(p, sid) == sid for p in range(30000, 30004)))
                            else:
                                self.assertTrue(requested_port == 29999 or model.service_ports.get(requested_port, sid) != sid)
                            continue
                        self.assertIn(port, range(30000, 30004))
                        self.assertEqual(model.service_ports.get(port, sid), sid, "service port handed out while owned by another session")
                        if requested_port is not None:
                            self.assertEqual(port, requested_port)
                        model.service_ports[port] = sid
                    elif op == "release-service":
                        port = gen.choice((30000, 30001, 30002, 30003, None))
                        release_service_port(repo_root=repo, machine_alias="m", session_id=sid, port=port)
                        if port is not None and model.service_ports.get(port) == sid:
                            del model.service_ports[port]
                    else:
                        release_all_session_leases(repo_root=repo, session_id=sid)
                        for table in (model.devices, model.ssh_ports, model.service_ports):
                            for key in [k for k, o in table.items() if o == sid]:
                                del table[key]
                    for other in self.SESSIONS:
                        self.assertEqual(session_live_leases(repo_root=repo, machine_alias="m", session_id=other), model.live(other), f"live leases for {other} diverge from the model")
                # Global invariant: no resource has two owners.
                seen: dict[tuple[str, int], str] = {}
                for other in self.SESSIONS:
                    for kind, values in session_live_leases(repo_root=repo, machine_alias="m", session_id=other).items():
                        for value in values:
                            self.assertNotIn((kind, value), seen, f"{kind} {value} owned by {seen.get((kind, value))} and {other}")
                            seen[(kind, value)] = other

        run_cases(120, body, label="lease model")

    def test_invalid_requests_are_rejected_before_touching_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with self.assertRaises(SessionStateError):
                allocate_session_leases(repo_root=repo, machine_alias="m", session_id="sess-a", requested_devices=[0], npu_count=1, available_devices=[0])
            with self.assertRaises(SessionStateError):
                allocate_session_leases(repo_root=repo, machine_alias="m", session_id="sess-a", npu_count=1, available_devices=None)
            with self.assertRaises((SessionStateError, ValidationError)):
                allocate_session_leases(repo_root=repo, machine_alias="m", session_id="sess-a", requested_devices=[-1], available_devices=[0])
            with self.assertRaises(SessionStateError):
                allocate_session_leases(repo_root=repo, machine_alias="m", session_id="!!", requested_devices=[0], available_devices=[0])
            self.assertFalse((repo / ".vaws-local" / "sessions" / "leases.json").exists(), "rejected requests must not create lease state")


class ConcurrentAllocationProperties(unittest.TestCase):
    def test_threads_never_receive_the_same_device(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                devices = list(range(gen.integer(1, 4)))
                sessions = [f"sess-{i}" for i in range(len(devices) + gen.integer(1, 2))]
                results: dict[str, Any] = {}

                def worker(sid: str) -> None:
                    try:
                        results[sid] = allocate_session_leases(repo_root=repo, machine_alias="m", session_id=sid, npu_count=1, available_devices=devices, port_available=lambda _p: True)
                    except SessionStateError as exc:
                        results[sid] = exc

                threads = [threading.Thread(target=worker, args=(sid,)) for sid in sessions]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                winners = {sid: r for sid, r in results.items() if isinstance(r, dict)}
                losers = {sid: r for sid, r in results.items() if not isinstance(r, dict)}
                self.assertEqual(len(winners), len(devices), f"exactly one winner per device expected: {results}")
                self.assertEqual(len(losers), len(sessions) - len(devices))
                allocated = sorted(d for r in winners.values() for d in r["npu_devices"])
                self.assertEqual(allocated, devices, "every device handed out exactly once")
                ports = [r["container_ssh_port"] for r in winners.values()]
                self.assertEqual(len(set(ports)), len(ports), "ssh ports must be unique")
                for sid in winners:
                    self.assertEqual(session_live_leases(repo_root=repo, machine_alias="m", session_id=sid)["npu_devices"], winners[sid]["npu_devices"])
                self.assertFalse(list((repo / ".vaws-local" / "sessions" / "locks").glob("*.lock")), "no lock left behind")

        run_cases(6, body, label="concurrent allocation")


class FileLockProperties(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock = Path(self._tmp.name) / "locks" / "leases.lock"

    def test_live_holder_excludes_others_until_release(self) -> None:
        with file_lock(self.lock, timeout_seconds=1):
            started = time.monotonic()
            with self.assertRaisesRegex(SessionStateError, "timed out"):
                with file_lock(self.lock, timeout_seconds=0.3, poll_seconds=0.02):
                    pass
            self.assertGreaterEqual(time.monotonic() - started, 0.25)
            self.assertTrue(self.lock.exists())
        self.assertFalse(self.lock.exists(), "lock must be released on exit")
        with file_lock(self.lock, timeout_seconds=1):
            pass

    def test_crashed_holder_is_unavailable_before_and_recovered_after_the_stale_window(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            self.lock.parent.mkdir(parents=True, exist_ok=True)
            self.lock.write_text('{"pid": 999999, "hostname": "example.invalid"}', encoding="utf-8")
            stale_after = gen.integer(2, 3600)
            age = gen.integer(0, 2 * stale_after)
            past = time.time() - age
            os.utime(self.lock, (past, past))
            if age >= stale_after:
                with file_lock(self.lock, timeout_seconds=0.2, poll_seconds=0.01, stale_after_seconds=stale_after):
                    self.assertTrue(self.lock.exists())
                self.assertFalse(self.lock.exists())
            else:
                with self.assertRaisesRegex(SessionStateError, "timed out"):
                    with file_lock(self.lock, timeout_seconds=0.05, poll_seconds=0.01, stale_after_seconds=stale_after):
                        pass
                self.assertTrue(self.lock.exists(), "a fresh crashed holder's lock must not be removed")
                self.lock.unlink()

        run_cases(40, body, label="stale lock window")

    def test_exception_inside_the_critical_section_releases_the_lock(self) -> None:
        with self.assertRaises(RuntimeError):
            with file_lock(self.lock, timeout_seconds=1):
                raise RuntimeError("boom")
        self.assertFalse(self.lock.exists())

    @unittest.expectedFailure
    def test_known_defect_two_waiters_can_both_acquire_after_removing_a_stale_lock(self) -> None:
        """KNOWN DEFECT (medium): stale-lock recovery is check-then-unlink
        without atomicity. Interleaving: A and B both stat the stale lock; A
        unlinks it and creates a fresh lock; B (still acting on its stale
        verdict) unlinks *A's* fresh lock and creates its own. Both are now
        inside ``file_lock`` at once, so two ``allocate_session_leases`` calls
        can hand the same NPU device to two sessions. Requires a crashed
        holder (>= 6h by default) plus concurrent recovery; consequence is a
        double lease. Reproduced with event ordering, not timing.
        Evidence: ``holders == ['A', 'B']`` before either release."""
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("{}", encoding="utf-8")
        old = time.time() - 7 * 3600
        os.utime(self.lock, (old, old))
        b_at_unlink = threading.Event()
        a_holds = threading.Event()
        holders: list[str] = []
        overlap: list[bool] = []
        real_unlink = Path.unlink

        # Patch the pathlib method (not os.unlink) so the hook works on every
        # Python version; before 3.11 pathlib bound os.unlink at import time.
        def patched_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
            if str(path) == str(self.lock):
                name = threading.current_thread().name
                if name == "waiter-B":
                    b_at_unlink.set()
                    a_holds.wait(5)
                elif name == "waiter-A":
                    b_at_unlink.wait(5)
            return real_unlink(path, *args, **kwargs)

        def worker(name: str) -> None:
            with file_lock(self.lock, timeout_seconds=5, poll_seconds=0.01):
                holders.append(name)
                if name == "waiter-A":
                    a_holds.set()
                    time.sleep(0.3)
                    overlap.append("waiter-B" in holders)
                else:
                    time.sleep(0.1)

        with mock.patch.object(Path, "unlink", patched_unlink):
            threads = [threading.Thread(target=worker, args=(n,), name=n) for n in ("waiter-A", "waiter-B")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(holders, ["waiter-A", "waiter-B"])
        self.assertEqual(overlap, [False], "B entered the critical section while A still held the lock")


class TokenProperties(unittest.TestCase):
    def test_port_range_parsing_boundary(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            start = gen.integer(-5, 70000)
            end = gen.integer(-5, 70000)
            value = f"{start}:{end}"
            valid = 0 < start <= end <= 65535
            if valid:
                self.assertEqual(parse_port_range(value), (start, end))
            else:
                with self.assertRaises(SessionStateError):
                    parse_port_range(value)
            with self.assertRaises((SessionStateError, ValueError)):
                parse_port_range(gen.choice((f"{start}", f"{start}-{end}", "", "a:b", f"{start}:{end}:1")))

        run_cases(300, body, label="parse_port_range")

    def test_container_names_are_docker_safe_and_bounded(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            namespace = gen.choice((None, gen.text(ID_CHARS, 0, 40)))
            sid = normalize_session_id(gen.text(ID_CHARS, 3, 90)) or "sess-fallback"
            name = session_container_name(namespace, sid)
            self.assertLessEqual(len(name), 63)
            self.assertRegex(name, r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
            self.assertTrue(name.startswith("vaws-"))
            self.assertEqual(session_container_name(namespace, sid), name)
            token = safe_token(gen.text(ID_CHARS, 0, 120), max_len=gen.integer(1, 63))
            self.assertRegex(token, r"^[A-Za-z0-9_.-]+$")

        run_cases(300, body, label="container names")


if __name__ == "__main__":
    unittest.main()
