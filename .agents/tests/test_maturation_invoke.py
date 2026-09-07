#!/usr/bin/env python3
"""Local tests for interrupted maturation CLI mux isolation and routing.

These tests cover the consumer package: ``run_cli`` copies ``os.environ``
and, when ``kill_after_ms`` is set, adds ``REMOTE_DEV_SSH_MUX=0`` to that
copy before ``Popen``. CLI calls are launched through
``.agents/scripts/remote_dev.py tool``. They use fake subprocesses, a fake
transport under the pinned checkout, or test-owned local children only. They
do not contact a host, kill a shared SSH process, or claim that this package
alone fixes remote-dev #2: effective OpenSSH ``ControlMaster=no`` /
``ControlPath=none`` / ``ControlPersist=no`` requires the provider, which
root integrates separately.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / ".agents"
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from maturation.invoke import (  # noqa: E402
    LAUNCHER,
    CliResult,
    RemoteDevUnavailable,
    RemoteDevInvoker,
    apply_real_execution_environment,
    cli_tool_name,
    launcher_argv,
    run_cli,
)

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

    def test_invalid_kill_mode_fails_before_spawn(self) -> None:
        launched: list[RecordingPopen] = []
        with mock.patch("maturation.invoke.subprocess.Popen", _popen_factory(launched)):
            with self.assertRaises(ValueError):
                run_cli(["should-not-spawn"], {}, kill_after_ms=10, kill_mode="nope")
        self.assertEqual(launched, [])


PINNED_SOURCE = Path("/private/tmp/vaws-remote-dev-final-source")
PINNED_SHA = "b6acc21d147e369e771f1ff916973d74d667691e"
ROOT_ENV = "VAWS_REMOTE_DEV_ROOT"


class ExternalRoutingTests(unittest.TestCase):
    """Real-invoker routing against the locator/launcher, with no host connection."""

    def test_canonical_tool_names_and_launcher_argv(self) -> None:
        self.assertEqual(cli_tool_name("remote.bash"), "remote_bash")
        self.assertEqual(cli_tool_name("probe"), "remote_probe")
        self.assertEqual(cli_tool_name("remote_read.py"), "remote_read")
        argv = launcher_argv("remote.glob", python="/usr/bin/python3")
        self.assertEqual(argv, ["/usr/bin/python3", str(LAUNCHER), "tool", "remote_glob", "--input-json", "-"])

    def test_call_cli_uses_launcher_argv_and_json_payload(self) -> None:
        captured: dict[str, Any] = {}

        def fake_run_cli(argv: list[str], payload: Mapping[str, Any], **kwargs: Any) -> CliResult:
            captured["argv"] = list(argv)
            captured["payload"] = dict(payload)
            captured["kwargs"] = kwargs
            return CliResult(payload={"result": {"status": "ok"}}, returncode=0, killed=False, duration_ms=1)

        invoker = RemoteDevInvoker(python="/usr/bin/python3")
        with mock.patch("maturation.invoke.remote_dev_root", return_value=PINNED_SOURCE), mock.patch(
            "maturation.invoke.run_cli", fake_run_cli
        ):
            result = invoker.call_cli(
                "remote.bash",
                {"command": "printf hi", "host": "10.0.0.9", "port": 22},
                kill_after_ms=40,
                kill_mode="transport",
                timeout_s=9.0,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            captured["argv"],
            ["/usr/bin/python3", str(LAUNCHER), "tool", "remote_bash", "--input-json", "-"],
        )
        self.assertEqual(captured["payload"]["command"], "printf hi")
        self.assertEqual(captured["payload"]["host"], "10.0.0.9")
        self.assertEqual(captured["kwargs"]["kill_after_ms"], 40)
        self.assertEqual(captured["kwargs"]["kill_mode"], "transport")
        self.assertEqual(captured["kwargs"]["timeout_s"], 9.0)

    def test_missing_source_fails_before_remote_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "absent")
            with mock.patch.dict(os.environ, {ROOT_ENV: missing}, clear=False):
                invoker = RemoteDevInvoker()
                with self.assertRaises(RemoteDevUnavailable) as ctx:
                    invoker.call("remote.probe", {"host": "10.0.0.9", "port": 22})
                self.assertIn("bootstrap", str(ctx.exception))
                self.assertIn(ROOT_ENV, str(ctx.exception))
                with self.assertRaises(RemoteDevUnavailable):
                    invoker.call_cli("remote.probe", {"host": "10.0.0.9", "port": 22})

    def test_import_does_not_require_checkout_or_mutate_environment(self) -> None:
        code = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(AGENTS)!r})\n"
            f"sys.path.insert(0, {str(AGENTS / 'lib')!r})\n"
            f"os.environ[{ROOT_ENV!r}] = {str(Path('/no/such/remote-dev-checkout'))!r}\n"
            "before = dict(os.environ)\n"
            "import maturation.invoke as invoke\n"
            "assert dict(os.environ) == before\n"
            "assert invoke.RemoteDevInvoker is not None\n"
            "print('ok')\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok")

    def test_incompatible_cached_mcp_fails_without_eviction(self) -> None:
        if not PINNED_SOURCE.is_dir():
            self.skipTest("pinned remote-dev source is not present")
        code = (
            "import sys, types\n"
            f"sys.path.insert(0, {str(AGENTS)!r})\n"
            f"sys.path.insert(0, {str(AGENTS / 'lib')!r})\n"
            "fake = types.ModuleType('mcp')\n"
            "fake.__file__ = '/tmp/other-mcp/__init__.py'\n"
            "sys.modules['mcp'] = fake\n"
            "from maturation.invoke import RemoteDevInvoker, RemoteDevUnavailable\n"
            "try:\n"
            "    RemoteDevInvoker().call('remote.probe', {'host': '10.0.0.9', 'port': 22})\n"
            "except RemoteDevUnavailable as exc:\n"
            "    print('failed', str(exc))\n"
            "else:\n"
            "    raise SystemExit('expected configuration error')\n"
            "print('mcp-file', sys.modules['mcp'].__file__)\n"
        )
        env = {**os.environ, ROOT_ENV: str(PINNED_SOURCE)}
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("incompatible mcp already imported", proc.stdout)
        self.assertIn("/tmp/other-mcp/__init__.py", proc.stdout)
        self.assertIn("mcp-file /tmp/other-mcp/__init__.py", proc.stdout)

    def test_configured_checkout_provenance_in_fresh_interpreter(self) -> None:
        if not PINNED_SOURCE.is_dir():
            self.skipTest("pinned remote-dev source is not present")
        code = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(AGENTS)!r})\n"
            f"sys.path.insert(0, {str(AGENTS / 'lib')!r})\n"
            "from maturation.invoke import RemoteDevInvoker, apply_real_execution_environment\n"
            "checkout = apply_real_execution_environment()\n"
            "invoker = RemoteDevInvoker()\n"
            "invoker._dispatcher()\n"
            "import mcp.tools, core\n"
            "print(checkout)\n"
            "print(mcp.tools.__file__)\n"
            "print(core.__file__)\n"
        )
        env = {**os.environ, ROOT_ENV: str(PINNED_SOURCE)}
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        checkout, tools_file, core_file = proc.stdout.strip().splitlines()
        pinned = str(PINNED_SOURCE.resolve())
        self.assertEqual(checkout, pinned)
        self.assertTrue(tools_file.startswith(pinned), tools_file)
        self.assertTrue(core_file.startswith(pinned), core_file)

    def test_apply_keeps_caller_overrides_and_fills_state_runtime_resolver(self) -> None:
        if not PINNED_SOURCE.is_dir():
            self.skipTest("pinned remote-dev source is not present")
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "state")
            resolver = "/abs/plugin.py:setup"
            overrides = {
                ROOT_ENV: str(PINNED_SOURCE),
                "REMOTE_DEV_RUNTIME_ENV_FILE": "/etc/profile.d/custom.sh",
                "REMOTE_DEV_STATE_DIR": state,
                "REMOTE_DEV_RESOLVERS": resolver,
                MUX_VAR: "1",
            }
            with mock.patch.dict(os.environ, overrides, clear=False):
                parent_before = dict(os.environ)
                checkout = apply_real_execution_environment()
                self.assertEqual(checkout, PINNED_SOURCE.resolve())
                self.assertEqual(os.environ.get("REMOTE_DEV_RUNTIME_ENV_FILE"), "/etc/profile.d/custom.sh")
                self.assertEqual(os.environ.get("REMOTE_DEV_STATE_DIR"), state)
                self.assertEqual(os.environ.get("REMOTE_DEV_RESOLVERS"), resolver)
                self.assertEqual(os.environ.get(MUX_VAR), "1")
                self.assertEqual(os.environ.get(MUX_VAR), parent_before.get(MUX_VAR))

    def test_inprocess_dispatcher_uses_pinned_source_with_fake_transport(self) -> None:
        if not PINNED_SOURCE.is_dir():
            self.skipTest("pinned remote-dev source is not present")
        code = (
            "import json, os, subprocess, sys\n"
            "from unittest import mock\n"
            f"sys.path.insert(0, {str(AGENTS)!r})\n"
            f"sys.path.insert(0, {str(AGENTS / 'lib')!r})\n"
            "from maturation.invoke import RemoteDevInvoker, apply_real_execution_environment\n"
            "apply_real_execution_environment()\n"
            "invoker = RemoteDevInvoker()\n"
            "invoker._dispatcher()\n"
            "import core.ssh_transport as transport\n"
            "import mcp.tools\n"
            "def fake_run(argv, **kwargs):\n"
            "    output = json.dumps({'status': 'ok', 'summary': {'hostname': 'fixture', 'python': '3.9.9'}, 'fake_transport': True})\n"
            "    if kwargs.get('text'):\n"
            "        return subprocess.CompletedProcess(list(argv), 0, output, '')\n"
            "    return subprocess.CompletedProcess(list(argv), 0, output.encode(), b'')\n"
            "with mock.patch.object(transport.subprocess, 'run', fake_run):\n"
            "    payload = invoker.call('remote.probe', {'host': '10.0.0.9', 'port': 22222, 'user': 'fixture', 'root': '/tmp', 'cwd': '/tmp', 'runtime_env': False})\n"
            "print(mcp.tools.__file__)\n"
            "print(json.dumps({'status': payload['result']['status'], 'fake': payload['result'].get('probe', {}).get('fake_transport'), 'origin': mcp.tools.__file__}))\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                ROOT_ENV: str(PINNED_SOURCE),
                "REMOTE_DEV_STATE_DIR": str(Path(tmp) / "state"),
                "REMOTE_DEV_SSH_MUX_DIR": str(Path(tmp) / "mux"),
            }
            proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        tools_file, payload_line = proc.stdout.strip().splitlines()
        self.assertTrue(tools_file.startswith(str(PINNED_SOURCE.resolve())), tools_file)
        payload = json.loads(payload_line)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["fake"])


if __name__ == "__main__":
    unittest.main()
