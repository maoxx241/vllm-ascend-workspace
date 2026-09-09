#!/usr/bin/env python3
"""Property tests for session identity and locks.

Modules: ``.agents/lib/vaws_session_id.py`` and ``vaws_session_state.py``.

Properties:

* ``normalize_session_id`` is total, idempotent, deterministic, bounded and
  keeps distinct long inputs distinct;
* the file lock excludes a live holder, recovers a crashed (stale) holder, and
  never reads a crashed holder's lock as "available" before the stale window;
* two stale waiters never overlap after reclaiming under the sidecar gate;
* a release-timeout cannot unlink a replacement owner's lease;
* a sidecar open failure releases the process-local gate and does not leak
  an fd; flock and critical-section failures release acquired resources
  without unlinking a foreign lease; a body exception does not hide an
  unrelated lease-close I/O error;
* unsupported flock and a held gate beyond the deadline fail closed;
* cooperating processes exclude each other, and a crashed process's stale
  lease is recovered after the stale window.

Lock races are reproduced with event ordering rather than timing. NFS and
cross-host locking are out of scope.
"""

from __future__ import annotations

import errno
import fcntl
import os
import subprocess
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
from vaws_session_state import SessionStateError, file_lock, parse_port_range, safe_token, session_container_name  # noqa: E402
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


class FileLockProperties(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock = Path(self._tmp.name) / "locks" / "leases.lock"

    def _join(self, thread: threading.Thread, timeout: float = 8) -> None:
        thread.join(timeout)
        self.assertFalse(thread.is_alive(), f"{thread.name} did not finish within {timeout}s")

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

    def test_known_defect_two_waiters_can_both_acquire_after_removing_a_stale_lock(self) -> None:
        """Two waiters that both judge a lease stale must not overlap.

        The unguarded ``Path.stat`` verdict is still racy; reclaim re-checks
        inode and mtime under the sidecar gate, so a waiter that saw stale
        cannot unlink a newer holder's file. Hook ``Path.stat`` (not unlink)
        so B records a stale reading and waits until A holds; under the old
        check-then-unlink code both entered the section. NFS / cross-host
        locking is out of scope.
        """
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("{}", encoding="utf-8")
        old = time.time() - 7 * 3600
        os.utime(self.lock, (old, old))
        b_judged_stale = threading.Event()
        a_holds = threading.Event()
        in_cs = 0
        max_in_cs = 0
        cs_lock = threading.Lock()
        holders: list[str] = []
        real_stat = Path.stat
        b_stat_count = 0
        count_lock = threading.Lock()

        def patched_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
            nonlocal b_stat_count
            result = real_stat(path, *args, **kwargs)
            if str(path) != str(self.lock):
                return result
            name = threading.current_thread().name
            if name == "waiter-B":
                with count_lock:
                    b_stat_count += 1
                    first = b_stat_count == 1
                if first:
                    b_judged_stale.set()
                    a_holds.wait(5)
            elif name == "waiter-A":
                b_judged_stale.wait(5)
            return result

        def worker(name: str) -> None:
            nonlocal in_cs, max_in_cs
            with file_lock(self.lock, timeout_seconds=5, poll_seconds=0.01):
                with cs_lock:
                    in_cs += 1
                    max_in_cs = max(max_in_cs, in_cs)
                    holders.append(name)
                if name == "waiter-A":
                    a_holds.set()
                    time.sleep(0.2)
                else:
                    time.sleep(0.05)
                with cs_lock:
                    in_cs -= 1

        with mock.patch.object(Path, "stat", patched_stat):
            threads = [
                threading.Thread(target=worker, args=(n,), name=n)
                for n in ("waiter-A", "waiter-B")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                self._join(thread)
        self.assertEqual(max_in_cs, 1, f"critical section overlapped: {holders}")
        self.assertCountEqual(holders, ["waiter-A", "waiter-B"])

    def test_release_timeout_does_not_unlink_replacement_owner(self) -> None:
        """A release timeout must not delete a replacement owner's lease.

        Event order uses real threads, a real lease, and a real flock sidecar:

        1. A acquires the lease and finishes its critical-section body.
        2. B holds the guard; A's lease mtime is made stale.
        3. A's release waits for the guard and times out.
        4. If A takes an unguarded identity/unlink fallback, pause after the
           identity snapshot until B holds a new lease (the old race).
        5. B reclaims the stale lease under the gate, acquires a new lease,
           and stays in its critical section.
        6. A must not unlink B's inode.
        7. C must not enter while B still holds.
        """
        a_in_cs = threading.Event()
        b_holds_guard = threading.Event()
        a_releasing = threading.Event()
        a_release_done = threading.Event()
        b_in_cs = threading.Event()
        c_finished = threading.Event()
        errors: list[str] = []
        a_release_exc: list[BaseException] = []
        c_entered = False
        replacement_after_a = False
        b_saw_lease_in_cs = False
        real_lock_identity = state._lock_identity

        def wrapped_lock_identity(path: Path) -> tuple[int, int] | None:
            ident = real_lock_identity(path)
            if (
                threading.current_thread().name == "owner-A"
                and a_releasing.is_set()
                and not a_release_done.is_set()
            ):
                a_release_done.set()
                b_in_cs.wait(5)
            return ident

        def owner_a() -> None:
            nonlocal replacement_after_a
            try:
                with file_lock(
                    self.lock,
                    timeout_seconds=0.08,
                    poll_seconds=0.01,
                    stale_after_seconds=1,
                ):
                    a_in_cs.set()
                    if not b_holds_guard.wait(5):
                        errors.append("A: B never took the guard")
                    a_releasing.set()
            except SessionStateError as exc:
                a_release_exc.append(exc)
            except BaseException as exc:
                errors.append(f"A: unexpected {exc!r}")
            finally:
                replacement_after_a = self.lock.exists()
                a_release_done.set()

        def waiter_b() -> None:
            nonlocal b_saw_lease_in_cs
            try:
                if not a_in_cs.wait(5):
                    errors.append("B: A never entered")
                    return
                with state._reclaim_gate(
                    self.lock,
                    deadline=time.monotonic() + 8,
                    poll_seconds=0.01,
                ):
                    b_holds_guard.set()
                    past = time.time() - 100
                    os.utime(self.lock, (past, past))
                    if not a_release_done.wait(5):
                        errors.append("B: A never finished release")
                        return
                    state._reclaim_stale_lock(self.lock, 1)
                with file_lock(
                    self.lock,
                    timeout_seconds=5,
                    poll_seconds=0.01,
                    stale_after_seconds=1,
                ):
                    b_in_cs.set()
                    b_saw_lease_in_cs = self.lock.exists()
                    if not c_finished.wait(5):
                        errors.append("B: C never finished")
            except BaseException as exc:
                errors.append(f"B: {exc!r}")

        def waiter_c() -> None:
            nonlocal c_entered
            try:
                if not b_in_cs.wait(5):
                    errors.append("C: B never entered")
                    return
                try:
                    with file_lock(
                        self.lock,
                        timeout_seconds=0.3,
                        poll_seconds=0.02,
                        stale_after_seconds=1,
                    ):
                        c_entered = True
                except SessionStateError:
                    pass
            except BaseException as exc:
                errors.append(f"C: {exc!r}")
            finally:
                c_finished.set()

        with mock.patch.object(state, "_lock_identity", wrapped_lock_identity):
            threads = [
                threading.Thread(target=owner_a, name="owner-A"),
                threading.Thread(target=waiter_b, name="waiter-B"),
                threading.Thread(target=waiter_c, name="waiter-C"),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                self._join(thread)

        self.assertEqual(errors, [])
        self.assertTrue(a_release_exc, "A must fail closed on release timeout")
        self.assertIsInstance(a_release_exc[0], SessionStateError)
        self.assertIn("timed out", str(a_release_exc[0]))
        self.assertTrue(replacement_after_a or b_saw_lease_in_cs, "replacement exists after A release")
        self.assertTrue(b_in_cs.is_set())
        self.assertTrue(b_saw_lease_in_cs, "B must still hold its lease in the critical section")
        self.assertFalse(c_entered, "C in critical section while B still holds")
        self.assertFalse(state._thread_gate(self.lock).locked())

    def test_sidecar_open_failure_releases_thread_gate(self) -> None:
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        guard = state._guard_path(self.lock)
        opened_guard_fds: list[int] = []
        real_open = os.open
        real_close = os.close
        failed_once = False

        def wrapped_open(path: str | bytes | os.PathLike[str], flags: int, *args: Any, **kwargs: Any) -> int:
            nonlocal failed_once
            if os.path.normpath(str(path)) == os.path.normpath(str(guard)) and not failed_once:
                failed_once = True
                raise OSError(errno.EMFILE, "Too many open files")
            fd = real_open(path, flags, *args, **kwargs)
            if os.path.normpath(str(path)) == os.path.normpath(str(guard)):
                opened_guard_fds.append(fd)
            return fd

        def wrapped_close(fd: int) -> None:
            if fd in opened_guard_fds:
                opened_guard_fds.remove(fd)
            real_close(fd)

        gate = state._thread_gate(self.lock)
        with mock.patch.object(os, "open", wrapped_open), mock.patch.object(os, "close", wrapped_close):
            with self.assertRaises(OSError) as raised:
                with state._reclaim_gate(
                    self.lock, deadline=time.monotonic() + 1, poll_seconds=0.01
                ):
                    pass
            self.assertEqual(raised.exception.errno, errno.EMFILE)
            self.assertTrue(failed_once)
            self.assertFalse(gate.locked(), "thread_gate_still_locked")
            self.assertEqual(opened_guard_fds, [])
            with state._reclaim_gate(
                self.lock, deadline=time.monotonic() + 1, poll_seconds=0.01
            ):
                self.assertTrue(gate.locked())
            self.assertFalse(gate.locked())
            self.assertEqual(opened_guard_fds, [])

    def test_flock_error_during_release_keeps_lease_and_closes_fd(self) -> None:
        closed: list[int] = []
        lease_fd: dict[str, int] = {}
        real_open = os.open
        real_close = os.close
        real_flock = fcntl.flock

        def wrapped_open(path: str | bytes | os.PathLike[str], flags: int, *args: Any, **kwargs: Any) -> int:
            fd = real_open(path, flags, *args, **kwargs)
            if os.path.normpath(str(path)) == os.path.normpath(str(self.lock)):
                lease_fd["fd"] = fd
            return fd

        def wrapped_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        def wrapped_flock(fd: int, flags: int) -> None:
            if flags & fcntl.LOCK_EX:
                raise OSError(errno.EIO, "injected flock failure")
            real_flock(fd, flags)

        with mock.patch.object(os, "open", wrapped_open), mock.patch.object(os, "close", wrapped_close), mock.patch.object(fcntl, "flock", wrapped_flock):
            with self.assertRaises(OSError) as raised:
                with file_lock(self.lock, timeout_seconds=1, poll_seconds=0.01):
                    self.assertTrue(self.lock.exists())
            self.assertEqual(raised.exception.errno, errno.EIO)
        self.assertTrue(self.lock.exists(), "foreign or own lease must not be unlinked without the gate")
        self.assertIn(lease_fd["fd"], closed)
        self.assertFalse(state._thread_gate(self.lock).locked())

    def test_body_exception_preserved_when_release_gate_times_out(self) -> None:
        a_in_cs = threading.Event()
        b_holds_guard = threading.Event()
        a_done = threading.Event()
        errors: list[str] = []
        body_exc: list[BaseException] = []

        def owner_a() -> None:
            try:
                with file_lock(self.lock, timeout_seconds=0.08, poll_seconds=0.01):
                    a_in_cs.set()
                    if not b_holds_guard.wait(5):
                        errors.append("A: B never took the guard")
                    raise RuntimeError("boom")
            except RuntimeError as exc:
                body_exc.append(exc)
            except SessionStateError as exc:
                errors.append(f"A: body error hidden by {exc!r}")
            finally:
                a_done.set()

        def holder_b() -> None:
            try:
                if not a_in_cs.wait(5):
                    errors.append("B: A never entered")
                    return
                with state._reclaim_gate(
                    self.lock,
                    deadline=time.monotonic() + 8,
                    poll_seconds=0.01,
                ):
                    b_holds_guard.set()
                    if not a_done.wait(5):
                        errors.append("B: A never finished")
            except BaseException as exc:
                errors.append(f"B: {exc!r}")

        threads = [
            threading.Thread(target=owner_a, name="owner-A"),
            threading.Thread(target=holder_b, name="holder-B"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            self._join(thread)
        self.assertEqual(errors, [])
        self.assertTrue(body_exc)
        self.assertIsInstance(body_exc[0], RuntimeError)
        self.assertTrue(self.lock.exists(), "fail-closed release must retain the lease")
        self.assertFalse(state._thread_gate(self.lock).locked())

    def test_body_exception_does_not_suppress_lease_close_error(self) -> None:
        real_open = os.open
        real_close = os.close
        lease_fds: list[int] = []

        def wrapped_open(path: str | bytes | os.PathLike[str], flags: int, *args: Any, **kwargs: Any) -> int:
            fd = real_open(path, flags, *args, **kwargs)
            if os.path.normpath(str(path)) == os.path.normpath(str(self.lock)):
                lease_fds.append(fd)
            return fd

        def wrapped_close(fd: int) -> None:
            real_close(fd)
            if fd in lease_fds:
                raise OSError(errno.EIO, "injected-lease-close-error")

        with mock.patch.object(os, "open", wrapped_open), mock.patch.object(os, "close", wrapped_close):
            with self.assertRaises(BaseException) as raised:
                with file_lock(self.lock, timeout_seconds=0.05, poll_seconds=0.002):
                    raise ValueError("body-failure")

        chain: list[BaseException] = []
        current: BaseException | None = raised.exception
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            chain.append(current)
            current = current.__cause__ or current.__context__
        messages = [str(item) for item in chain]
        notes = [note for item in chain for note in getattr(item, "__notes__", [])]
        self.assertTrue(
            any("injected-lease-close-error" in message for message in messages)
            or any("injected-lease-close-error" in note for note in notes),
            f"close error missing from {chain!r}",
        )
        self.assertTrue(
            any(isinstance(item, ValueError) and "body-failure" in str(item) for item in chain),
            f"body error missing from {chain!r}",
        )
        self.assertFalse(state._thread_gate(self.lock).locked())

    def test_unsupported_flock_fails_closed_on_stale_reclaim(self) -> None:
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("{}", encoding="utf-8")
        past = time.time() - 100
        os.utime(self.lock, (past, past))

        def unsupported(_fd: int, _flags: int) -> None:
            raise OSError(errno.ENOTSUP, "Operation not supported")

        with mock.patch.object(fcntl, "flock", unsupported):
            with self.assertRaisesRegex(SessionStateError, "timed out"):
                with file_lock(
                    self.lock,
                    timeout_seconds=0.2,
                    poll_seconds=0.02,
                    stale_after_seconds=1,
                ):
                    pass
        self.assertTrue(self.lock.exists(), "unsupported flock must leave the stale lease in place")
        self.assertFalse(state._thread_gate(self.lock).locked())

    def test_unsupported_flock_fails_closed_on_release(self) -> None:
        def unsupported(_fd: int, _flags: int) -> None:
            raise OSError(errno.ENOTSUP, "Operation not supported")

        with mock.patch.object(fcntl, "flock", unsupported):
            with self.assertRaisesRegex(SessionStateError, "unsupported"):
                with file_lock(self.lock, timeout_seconds=1, poll_seconds=0.01):
                    self.assertTrue(self.lock.exists())
        self.assertTrue(self.lock.exists(), "release must not unlink without cooperating flock")
        self.assertFalse(state._thread_gate(self.lock).locked())

    def test_held_gate_beyond_deadline_leaves_stale_lease(self) -> None:
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("{}", encoding="utf-8")
        past = time.time() - 100
        os.utime(self.lock, (past, past))
        b_holds_guard = threading.Event()
        waiter_done = threading.Event()
        errors: list[str] = []
        waiter_exc: list[BaseException] = []

        def holder() -> None:
            try:
                with state._reclaim_gate(
                    self.lock,
                    deadline=time.monotonic() + 8,
                    poll_seconds=0.01,
                ):
                    b_holds_guard.set()
                    if not waiter_done.wait(5):
                        errors.append("holder: waiter never finished")
            except BaseException as exc:
                errors.append(f"holder: {exc!r}")

        def waiter() -> None:
            try:
                if not b_holds_guard.wait(5):
                    errors.append("waiter: holder never took the gate")
                    return
                with file_lock(
                    self.lock,
                    timeout_seconds=0.2,
                    poll_seconds=0.02,
                    stale_after_seconds=1,
                ):
                    errors.append("waiter: acquired under a held gate")
            except SessionStateError as exc:
                waiter_exc.append(exc)
            except BaseException as exc:
                errors.append(f"waiter: {exc!r}")
            finally:
                waiter_done.set()

        threads = [
            threading.Thread(target=holder, name="gate-holder"),
            threading.Thread(target=waiter, name="stale-waiter"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            self._join(thread)
        self.assertEqual(errors, [])
        self.assertTrue(waiter_exc)
        self.assertIn("timed out", str(waiter_exc[0]))
        self.assertTrue(self.lock.exists())
        self.assertFalse(state._thread_gate(self.lock).locked())

    def test_cooperating_processes_exclude_each_other(self) -> None:
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        ready = Path(self._tmp.name) / "child.ready"
        release = Path(self._tmp.name) / "child.release"
        proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--hold-lock",
                str(self.lock),
                str(ready),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    out, err = proc.communicate()
                    self.fail(f"child exited early rc={proc.returncode} stdout={out!r} stderr={err!r}")
                if ready.exists() and self.lock.exists():
                    break
                time.sleep(0.01)
            else:
                self.fail("child did not acquire the lease")
            with self.assertRaisesRegex(SessionStateError, "timed out"):
                with file_lock(self.lock, timeout_seconds=0.3, poll_seconds=0.02):
                    pass
            self.assertTrue(self.lock.exists())
            release.write_text("1", encoding="utf-8")
            self.assertEqual(proc.wait(timeout=5), 0)
            proc.communicate()
            with file_lock(self.lock, timeout_seconds=1, poll_seconds=0.01):
                self.assertTrue(self.lock.exists())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)

    def test_crashed_process_fresh_lease_excludes_then_stale_recovers(self) -> None:
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        ready = Path(self._tmp.name) / "crash.ready"
        proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--crash-hold-lock",
                str(self.lock),
                str(ready),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ready.exists() and self.lock.exists():
                    break
                if proc.poll() is not None and not ready.exists():
                    out, err = proc.communicate()
                    self.fail(f"child exited before ready rc={proc.returncode} stdout={out!r} stderr={err!r}")
                time.sleep(0.01)
            else:
                self.fail("child did not create a lease")
            self.assertEqual(proc.wait(timeout=5), 0)
            proc.communicate()
            with self.assertRaisesRegex(SessionStateError, "timed out"):
                with file_lock(
                    self.lock,
                    timeout_seconds=0.15,
                    poll_seconds=0.02,
                    stale_after_seconds=60,
                ):
                    pass
            self.assertTrue(self.lock.exists(), "a fresh crashed holder's lock must not be removed")
            past = time.time() - 100
            os.utime(self.lock, (past, past))
            with file_lock(
                self.lock,
                timeout_seconds=1,
                poll_seconds=0.01,
                stale_after_seconds=1,
            ):
                self.assertTrue(self.lock.exists())
            self.assertFalse(self.lock.exists())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)


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


def _lock_worker(mode: str, lock: Path, ready: Path, release: Path | None) -> None:
    if mode == "--crash-hold-lock":
        with file_lock(lock, timeout_seconds=5, poll_seconds=0.01):
            ready.write_text("1", encoding="utf-8")
            os._exit(0)
    with file_lock(lock, timeout_seconds=5, poll_seconds=0.01):
        ready.write_text("1", encoding="utf-8")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if release is not None and release.exists():
                return
            time.sleep(0.01)
        raise SystemExit("release signal not seen")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] in {"--hold-lock", "--crash-hold-lock"}:
        release_path = Path(sys.argv[4]) if len(sys.argv) > 4 else None
        _lock_worker(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), release_path)
        raise SystemExit(0)
    unittest.main()
