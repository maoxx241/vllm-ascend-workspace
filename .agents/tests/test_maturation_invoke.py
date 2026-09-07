#!/usr/bin/env python3
"""Local tests for interrupted maturation CLI mux isolation.

These tests cover the consumer package: ``run_cli`` copies ``os.environ``
and, when ``kill_after_ms`` is set, adds ``REMOTE_DEV_SSH_MUX=0`` to that
copy before ``Popen``. They use fake subprocesses or test-owned local
children only. They do not contact a host, kill a shared SSH process, or
claim that this package alone fixes remote-dev #2: effective OpenSSH
``ControlMaster=no`` / ``ControlPath=none`` / ``ControlPersist=no``
requires the provider candidate, which root integrates separately.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / ".agents"
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from maturation.invoke import run_cli  # noqa: E402

MUX_VAR = "REMOTE_DEV_SSH_MUX"
MUX_OFF = "0"
DUMP_CHILD = (
    "import json,os,sys; "
    "json.dump({'mux': os.environ.get('REMOTE_DEV_SSH_MUX')}, sys.stdout)"
)


def _mux_parent_bytes() -> bytes | None:
    if hasattr(os, "environb"):
        return os.environb.get(b"REMOTE_DEV_SSH_MUX")
    value = os.environ.get(MUX_VAR)
    return None if value is None else value.encode("utf-8")


@contextmanager
def parent_mux(value: str | None) -> Iterator[bytes | None]:
    had = MUX_VAR in os.environ
    saved = os.environ.get(MUX_VAR)
    try:
        if value is None:
            os.environ.pop(MUX_VAR, None)
        else:
            os.environ[MUX_VAR] = value
        yield _mux_parent_bytes()
    finally:
        if had:
            assert saved is not None
            os.environ[MUX_VAR] = saved
        else:
            os.environ.pop(MUX_VAR, None)


class RecordingPopen:
    """In-process stand-in for ``subprocess.Popen`` used by ``run_cli``."""

    def __init__(
        self,
        argv: list[str],
        *,
        expire_first: bool = False,
        barrier: threading.Barrier | None = None,
        launched: list[RecordingPopen] | None = None,
        **kwargs: Any,
    ) -> None:
        self.argv = list(argv)
        self.env = kwargs.get("env")
        self.env_at_start = dict(self.env or {})
        self.kwargs = kwargs
        self.pid = 82000 + (0 if launched is None else len(launched))
        self.returncode: int | None = None
        self.communicate_calls = 0
        self.expire_first = expire_first
        if launched is not None:
            launched.append(self)
        if barrier is not None:
            barrier.wait(timeout=5)

    def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        if self.expire_first and self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(self.argv, timeout if timeout is not None else 0)
        if self.returncode is None:
            self.returncode = -9 if self.expire_first else 0
        return b'{"ok": true}', b""


def _popen_factory(
    launched: list[RecordingPopen],
    *,
    expire_first: bool = False,
    barrier: threading.Barrier | None = None,
    expire_argv: str | None = None,
) -> type:
    class FactoryPopen(RecordingPopen):
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            expire = expire_first
            if expire_argv is not None:
                expire = bool(argv) and argv[0] == expire_argv
            super().__init__(
                argv,
                expire_first=expire,
                barrier=barrier,
                launched=launched,
                **kwargs,
            )

    return FactoryPopen


class InterruptedCliMuxTests(unittest.TestCase):
    def _kill_recorders(self, launched: list[RecordingPopen]) -> tuple[list[tuple[str, int, dict[str, str]]], Any, Any]:
        killed: list[tuple[str, int, dict[str, str]]] = []

        def kill_pg(proc: RecordingPopen) -> None:
            env = dict(proc.env or {})
            killed.append(("wrapper", proc.pid, env))
            proc.returncode = -9

        def kill_ssh(pid: int) -> list[int]:
            match = next(item for item in launched if item.pid == pid)
            killed.append(("transport", pid, dict(match.env or {})))
            return []

        return killed, kill_pg, kill_ssh

    def _run_interrupt(
        self,
        *,
        kill_mode: str,
        expire_first: bool = True,
        parent: str | None = None,
        kill_after_ms: int | None = 40,
        timeout_s: float = 5.0,
        argv: list[str] | None = None,
    ) -> tuple[list[RecordingPopen], list[tuple[str, int, dict[str, str]]], bytes | None, dict[str, str]]:
        launched: list[RecordingPopen] = []
        killed, kill_pg, kill_ssh = self._kill_recorders(launched)
        with parent_mux(parent) as mux_before:
            parent_before = dict(os.environ)
            popen_patch = mock.patch(
                "maturation.invoke.subprocess.Popen",
                _popen_factory(launched, expire_first=expire_first),
            )
            with popen_patch, mock.patch("maturation.invoke._kill_process_group", kill_pg), mock.patch(
                "maturation.invoke._kill_ssh_children", kill_ssh
            ):
                run_cli(
                    argv or ["interrupted-child"],
                    {},
                    kill_after_ms=kill_after_ms,
                    kill_mode=kill_mode,
                    timeout_s=timeout_s,
                )
            parent_after = dict(os.environ)
            mux_after = _mux_parent_bytes()
        self.assertEqual(parent_after, parent_before)
        self.assertEqual(mux_after, mux_before)
        return launched, killed, mux_before, parent_before

    def test_popen_env_opts_out_of_mux_before_kill_for_both_modes(self) -> None:
        for kill_mode in ("transport", "wrapper"):
            with self.subTest(kill_mode=kill_mode):
                launched, killed, _, _ = self._run_interrupt(kill_mode=kill_mode)
                self.assertEqual(len(launched), 1)
                child = launched[0]
                self.assertEqual(child.env_at_start.get(MUX_VAR), MUX_OFF)
                self.assertEqual((child.env or {}).get(MUX_VAR), MUX_OFF)
                self.assertTrue(child.kwargs.get("start_new_session"))
                self.assertEqual(len(killed), 1)
                self.assertEqual(killed[0][0], kill_mode)
                self.assertEqual(killed[0][1], child.pid)
                self.assertEqual(killed[0][2].get(MUX_VAR), MUX_OFF)

    def test_ordinary_cli_preserves_unset_and_inherited_mux_and_parent_bytes(self) -> None:
        for inherited in (None, "1", "0"):
            with self.subTest(inherited=inherited):
                launched: list[RecordingPopen] = []
                with parent_mux(inherited) as mux_before:
                    parent_before = dict(os.environ)
                    popen_patch = mock.patch(
                        "maturation.invoke.subprocess.Popen",
                        _popen_factory(launched, expire_first=False),
                    )
                    with popen_patch, mock.patch("maturation.invoke._kill_process_group") as kill_pg, mock.patch(
                        "maturation.invoke._kill_ssh_children"
                    ) as kill_ssh:
                        run_cli(["ordinary-child"], {}, timeout_s=5.0)
                    self.assertEqual(dict(os.environ), parent_before)
                    self.assertEqual(_mux_parent_bytes(), mux_before)
                self.assertEqual(len(launched), 1)
                env = launched[0].env
                self.assertIsInstance(env, dict)
                assert env is not None
                self.assertIsNot(env, os.environ)
                if inherited is None:
                    self.assertNotIn(MUX_VAR, env)
                else:
                    self.assertEqual(env.get(MUX_VAR), inherited)
                kill_pg.assert_not_called()
                kill_ssh.assert_not_called()

    def test_timeout_kill_uses_already_isolated_child_only(self) -> None:
        outside_pid = 1
        for kill_mode in ("transport", "wrapper"):
            with self.subTest(kill_mode=kill_mode):
                launched, killed, _, parent_before = self._run_interrupt(kill_mode=kill_mode, parent="1")
                child = launched[0]
                self.assertEqual(child.env_at_start.get(MUX_VAR), MUX_OFF)
                self.assertEqual((child.env or {}).get(MUX_VAR), MUX_OFF)
                self.assertEqual([item[1] for item in killed], [child.pid])
                self.assertNotIn(outside_pid, [item[1] for item in killed])
                self.assertIsNot(child.env, os.environ)
                self.assertEqual(parent_before.get(MUX_VAR), "1")

    def test_concurrent_interrupt_and_neighbor_keep_distinct_env_mappings(self) -> None:
        launched: list[RecordingPopen] = []
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        killed, kill_pg, kill_ssh = self._kill_recorders(launched)

        def interrupt() -> None:
            try:
                run_cli(["interrupt"], {}, kill_after_ms=40, kill_mode="wrapper", timeout_s=5.0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def neighbor() -> None:
            try:
                run_cli(["neighbor"], {}, timeout_s=5.0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with parent_mux("1") as mux_before:
            parent_before = dict(os.environ)
            popen_patch = mock.patch(
                "maturation.invoke.subprocess.Popen",
                _popen_factory(launched, barrier=barrier, expire_argv="interrupt"),
            )
            with popen_patch, mock.patch("maturation.invoke._kill_process_group", kill_pg), mock.patch(
                "maturation.invoke._kill_ssh_children", kill_ssh
            ):
                interrupt_thread = threading.Thread(target=interrupt)
                neighbor_thread = threading.Thread(target=neighbor)
                interrupt_thread.start()
                neighbor_thread.start()
                interrupt_thread.join(timeout=5)
                neighbor_thread.join(timeout=5)
            self.assertEqual(dict(os.environ), parent_before)
            self.assertEqual(_mux_parent_bytes(), mux_before)

        self.assertEqual(errors, [])
        self.assertFalse(interrupt_thread.is_alive())
        self.assertFalse(neighbor_thread.is_alive())
        by_argv = {proc.argv[0]: proc for proc in launched}
        self.assertEqual(set(by_argv), {"interrupt", "neighbor"})
        interrupt_env = by_argv["interrupt"].env
        neighbor_env = by_argv["neighbor"].env
        self.assertIsInstance(interrupt_env, dict)
        self.assertIsInstance(neighbor_env, dict)
        assert interrupt_env is not None
        assert neighbor_env is not None
        self.assertIsNot(interrupt_env, neighbor_env)
        self.assertEqual(by_argv["interrupt"].env_at_start.get(MUX_VAR), MUX_OFF)
        self.assertEqual(interrupt_env.get(MUX_VAR), MUX_OFF)
        self.assertEqual(by_argv["neighbor"].env_at_start.get(MUX_VAR), "1")
        self.assertEqual(neighbor_env.get(MUX_VAR), "1")
        self.assertEqual([item[0] for item in killed], ["wrapper"])
        self.assertEqual(killed[0][1], by_argv["interrupt"].pid)
        self.assertEqual(killed[0][2].get(MUX_VAR), MUX_OFF)

    def test_ordinary_deadline_does_not_opt_out_of_mux(self) -> None:
        launched: list[RecordingPopen] = []
        killed, kill_pg, kill_ssh = self._kill_recorders(launched)
        with parent_mux("1"):
            parent_before = dict(os.environ)
            mux_before = _mux_parent_bytes()
            popen_patch = mock.patch(
                "maturation.invoke.subprocess.Popen",
                _popen_factory(launched, expire_first=True),
            )
            with popen_patch, mock.patch("maturation.invoke._kill_process_group", kill_pg), mock.patch(
                "maturation.invoke._kill_ssh_children", kill_ssh
            ):
                result = run_cli(["deadline-child"], {}, timeout_s=0.01)
            self.assertEqual(dict(os.environ), parent_before)
            self.assertEqual(_mux_parent_bytes(), mux_before)
        self.assertTrue(result.killed)
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0].env.get(MUX_VAR) if launched[0].env else None, "1")
        self.assertEqual([item[0] for item in killed], ["wrapper"])

    def test_real_local_child_sees_isolated_or_inherited_mux(self) -> None:
        argv = [sys.executable, "-c", DUMP_CHILD]
        with parent_mux(None):
            isolated = run_cli(argv, {}, kill_after_ms=10_000, kill_mode="transport")
            ordinary = run_cli(argv, {}, timeout_s=10.0)
            self.assertNotIn(MUX_VAR, os.environ)
        self.assertEqual(json.loads(isolated.stdout_tail), {"mux": MUX_OFF})
        self.assertEqual(json.loads(ordinary.stdout_tail), {"mux": None})
        with parent_mux("1"):
            inherited = run_cli(argv, {}, timeout_s=10.0)
            isolated_over_inherit = run_cli(argv, {}, kill_after_ms=10_000, kill_mode="wrapper")
            self.assertEqual(os.environ.get(MUX_VAR), "1")
        self.assertEqual(json.loads(inherited.stdout_tail), {"mux": "1"})
        self.assertEqual(json.loads(isolated_over_inherit.stdout_tail), {"mux": MUX_OFF})


if __name__ == "__main__":
    unittest.main()
