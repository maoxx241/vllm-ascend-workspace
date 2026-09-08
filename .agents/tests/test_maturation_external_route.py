#!/usr/bin/env python3
"""Joint consumer/provider routing tests with a fake transport and no host.

The four-case mux fixture is the accepted #93 independent joint, with the
installed ``vaws-remote-dev`` package as the provider. The second joint goes
through ``RemoteDevInvoker.call_cli`` and ``python -m remote_dev``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / ".agents"
LIB = AGENTS / "lib"
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from maturation.invoke import RemoteDevInvoker, run_cli  # noqa: E402
from maturation_provider import require_optional_provider, require_pinned_provider  # noqa: E402

MUX_CHILD = AGENTS / "tests" / "maturation_mux_child.py"
MUX_VAR = "REMOTE_DEV_SSH_MUX"

SITECUSTOMIZE = r'''
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_original_run = subprocess.run
_is_launcher = False
_calls = []
_trace_dir = os.environ.get("MATURATION_FAKE_TRACE_DIR")
_gate = os.environ.get("MATURATION_FAKE_GATE")


def _write_trace():
    if _is_launcher or not _trace_dir:
        return
    Path(_trace_dir).mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "override": os.environ.get("REMOTE_DEV_SSH_MUX"),
        "argv": list(sys.argv),
        "calls": _calls,
        "state_dir": os.environ.get("REMOTE_DEV_STATE_DIR"),
        "runtime_env_file": os.environ.get("REMOTE_DEV_RUNTIME_ENV_FILE"),
        "resolvers": os.environ.get("REMOTE_DEV_RESOLVERS"),
        "root": os.environ.get("VAWS_" + "REMOTE_DEV_ROOT"),
        "cli": list(sys.argv),
    }
    Path(_trace_dir, str(os.getpid()) + ".json").write_text(json.dumps(payload), encoding="utf-8")


def fake_run(argv, **kwargs):
    argv = list(argv)
    name = Path(str(argv[0])).name if argv else ""
    if name != "ssh":
        return _original_run(argv, **kwargs)
    _calls.append({"argv": argv})
    _write_trace()
    if (not _is_launcher) and os.environ.get("REMOTE_DEV_SSH_MUX") == "0" and _gate:
        deadline = time.monotonic() + 5
        while not Path(_gate).exists():
            if time.monotonic() > deadline:
                raise RuntimeError("test-owned gate was not released")
            time.sleep(0.005)
    output = json.dumps({"status": "ok", "summary": {"hostname": "fixture", "python": "3.9.9"}, "fake_transport": True})
    if kwargs.get("text"):
        return subprocess.CompletedProcess(argv, 0, output, "")
    return subprocess.CompletedProcess(argv, 0, output.encode(), b"")


subprocess.run = fake_run
'''


def mux_options(trace: dict[str, Any]) -> list[list[str]]:
    return [
        [arg for arg in call["argv"] if arg.startswith(("ControlMaster=", "ControlPath=", "ControlPersist="))]
        for call in trace["calls"]
    ]


def _wait_for(path: Path, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"did not appear: {path}")
        time.sleep(0.005)


class FourCaseMuxJointTests(unittest.TestCase):
    """Accepted #93 joint fixture; provider comes from the scaffold locator.

    Integration: skips when no remote-dev checkout exists; must pass when one
    is present and pinned.
    """

    def setUp(self) -> None:
        self.provider = require_optional_provider()

    def test_four_inherited_mode_cases_with_pinned_provider(self) -> None:
        original_env = dict(os.environ)
        records: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="mux-exact-integration-") as temporary:
            root = Path(temporary)
            for inherited in (None, "1"):
                for mode in ("transport", "wrapper"):
                    case = root / ((inherited or "unset") + "-" + mode)
                    case.mkdir()
                    gate = case / "release"
                    interrupted_trace = case / "interrupted.json"
                    neighbor_trace = case / "neighbor.json"
                    signal_requests: list[dict[str, Any]] = []
                    interrupted_mux_dir = case / "interrupted-mux-must-not-exist"
                    payload = {"mux_dir": str(interrupted_mux_dir), "trace": str(interrupted_trace), "gate": str(gate)}
                    argv = [sys.executable, str(MUX_CHILD)]

                    def fake_signal(target: Any, current_mode: str = mode) -> list[int]:
                        pid = target if isinstance(target, int) else target.pid
                        trace = json.loads(interrupted_trace.read_text())
                        self.assertEqual(pid, trace["pid"])
                        self.assertEqual(trace["override"], "0")
                        self.assertEqual(
                            mux_options(trace),
                            [["ControlMaster=no", "ControlPath=none", "ControlPersist=no"]] * 3,
                        )
                        self.assertFalse(interrupted_mux_dir.exists())
                        signal_requests.append({"pid": pid, "mode": current_mode, "mocked": True})
                        gate.write_text("release test-owned fixture")
                        return []

                    with mock.patch.dict(os.environ, {}, clear=False):
                        if inherited is None:
                            os.environ.pop(MUX_VAR, None)
                        else:
                            os.environ[MUX_VAR] = inherited
                        before = dict(os.environ)
                        with mock.patch("maturation.invoke._kill_ssh_children", fake_signal), mock.patch(
                            "maturation.invoke._kill_process_group", fake_signal
                        ), ThreadPoolExecutor(max_workers=1) as pool:
                            interrupted = pool.submit(
                                run_cli,
                                argv,
                                payload,
                                kill_after_ms=600,
                                kill_mode=mode,
                                timeout_s=3,
                                cwd=case,
                            )
                            _wait_for(interrupted_trace)
                            self.assertFalse(gate.exists(), "interruption happened before neighbor started")
                            neighbor = run_cli(
                                argv,
                                {"mux_dir": str(case / "neighbor-mux"), "trace": str(neighbor_trace)},
                                timeout_s=3,
                                cwd=case,
                            )
                            self.assertFalse(gate.exists(), "neighbor did not overlap the waiting child")
                            result = interrupted.result(timeout=5)
                        self.assertEqual(dict(os.environ), before, "consumer changed parent environment")
                    self.assertEqual(dict(os.environ), original_env)
                    child_trace = json.loads(interrupted_trace.read_text())
                    neighbor_record = json.loads(neighbor_trace.read_text())
                    self.assertTrue(result.killed)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(len(signal_requests), 1)
                    self.assertFalse(neighbor.killed)
                    self.assertEqual(neighbor.returncode, 0)
                    self.assertEqual(child_trace["override"], "0")
                    self.assertIsNone(child_trace["mux_ready"])
                    self.assertEqual(neighbor_record["override"], inherited)
                    self.assertEqual(len(neighbor_record["calls"]), 3)
                    for options in mux_options(neighbor_record):
                        self.assertEqual(options[0], "ControlMaster=auto")
                        self.assertTrue(options[1].startswith("ControlPath=" + str(case / "neighbor-mux") + "/%C-"))
                        self.assertEqual(options[2], "ControlPersist=120")
                    self.assertFalse(interrupted_mux_dir.exists())
                    records.append(
                        {
                            "inherited": inherited,
                            "mode": mode,
                            "interrupted": child_trace,
                            "neighbor": neighbor_record,
                            "signal_requests": signal_requests,
                            "parent_environment_unchanged": True,
                        }
                    )
        self.assertEqual(len(records), 4)


class LauncherJointRouteTests(unittest.TestCase):
    """``call_cli`` through the real launcher into the pinned tools wrapper.

    Integration: skips when no remote-dev checkout exists; must pass when one
    is present and pinned.
    """

    def setUp(self) -> None:
        self.provider = require_optional_provider()

    def test_launcher_execve_preserves_mux_and_matches_inprocess_source(self) -> None:
        original_env = dict(os.environ)
        probe_args = {
            "host": "192.0.2.9",
            "port": 22222,
            "user": "fixture",
            "root": "/tmp",
            "cwd": "/tmp",
            "runtime_env": False,
            "timeout_ms": 5000,
        }
        with tempfile.TemporaryDirectory(prefix="mux-launcher-joint-") as temporary:
            root = Path(temporary)
            site = root / "site"
            site.mkdir()
            (site / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
            state = root / "state"
            mux_dir = root / "neighbor-mux"
            pythonpath = str(site)
            existing = os.environ.get("PYTHONPATH")
            if existing:
                pythonpath = pythonpath + os.pathsep + existing
            launched_argv: list[list[str]] = []
            real_popen = subprocess.Popen

            def tracking_popen(argv: list[str], **kwargs: Any) -> Any:
                launched_argv.append(list(argv))
                return real_popen(argv, **kwargs)

            records: list[dict[str, Any]] = []
            for inherited in (None, "1"):
                for mode in ("transport", "wrapper"):
                    case = root / ((inherited or "unset") + "-" + mode)
                    case.mkdir()
                    gate = case / "release"
                    trace_dir = case / "traces"
                    trace_dir.mkdir()
                    signal_requests: list[dict[str, Any]] = []

                    def fake_signal(target: Any, current_mode: str = mode, current_traces: Path = trace_dir, current_gate: Path = gate) -> list[int]:
                        pid = target if isinstance(target, int) else target.pid
                        path = current_traces / f"{pid}.json"
                        self.assertTrue(path.exists(), "timeout callback before wrapper recorded argv")
                        trace = json.loads(path.read_text(encoding="utf-8"))
                        self.assertEqual(pid, trace["pid"])
                        self.assertEqual(trace["override"], "0")
                        self.assertEqual(
                            mux_options(trace),
                            [["ControlMaster=no", "ControlPath=none", "ControlPersist=no"]],
                        )
                        self.assertTrue(any(item in {"probe", "remote-probe", "remote_probe"} or str(item).endswith("probe") for item in trace["argv"]))
                        signal_requests.append({"pid": pid, "mode": current_mode, "mocked": True})
                        current_gate.write_text("release test-owned fixture")
                        return []

                    child_env = {
                        "PYTHONPATH": pythonpath,
                        "PYTHONUNBUFFERED": "1",
                        "REMOTE_DEV_STATE_DIR": str(state / (inherited or "unset") / mode),
                        "REMOTE_DEV_SSH_MUX_DIR": str(mux_dir),
                        "REMOTE_DEV_RUNTIME_ENV_FILE": "/etc/profile.d/vaws-ascend-env.sh",
                        "MATURATION_FAKE_TRACE_DIR": str(trace_dir),
                        "MATURATION_FAKE_GATE": str(gate),
                    }
                    launched_argv.clear()
                    with mock.patch.dict(os.environ, child_env, clear=False):
                        if inherited is None:
                            os.environ.pop(MUX_VAR, None)
                        else:
                            os.environ[MUX_VAR] = inherited
                        before = dict(os.environ)
                        invoker = RemoteDevInvoker()
                        with mock.patch("maturation.invoke.subprocess.Popen", tracking_popen), mock.patch(
                            "maturation.invoke._kill_ssh_children", fake_signal
                        ), mock.patch("maturation.invoke._kill_process_group", fake_signal), ThreadPoolExecutor(
                            max_workers=1
                        ) as pool:
                            interrupted = pool.submit(
                                invoker.call_cli,
                                "remote.probe",
                                probe_args,
                                kill_after_ms=800,
                                kill_mode=mode,
                                timeout_s=5,
                            )
                            deadline = time.monotonic() + 2
                            interrupted_path = None
                            while time.monotonic() < deadline:
                                files = list(trace_dir.glob("*.json"))
                                if files:
                                    interrupted_path = files[0]
                                    break
                                time.sleep(0.005)
                            self.assertIsNotNone(interrupted_path, "child did not record provider argv")
                            self.assertFalse(gate.exists(), "interruption happened before neighbor started")
                            neighbor = invoker.call_cli("remote.probe", probe_args, timeout_s=5)
                            self.assertFalse(gate.exists(), "neighbor did not overlap the waiting child")
                            result = interrupted.result(timeout=5)
                        self.assertEqual(dict(os.environ), before, "consumer changed parent environment")
                    self.assertTrue(launched_argv)
                    launcher_calls = [
                        argv for argv in launched_argv if "-m" in argv and "remote_dev" in argv
                    ]
                    self.assertTrue(launcher_calls, launched_argv)
                    self.assertEqual(launcher_calls[0][2:6], ["remote_dev", "probe", "--input-json", "-"])
                    self.assertTrue(result.killed)
                    self.assertEqual(len(signal_requests), 1)
                    self.assertFalse(neighbor.killed)
                    self.assertEqual(neighbor.returncode, 0)
                    neighbor_result = neighbor.result or {}
                    self.assertEqual(neighbor_result.get("status"), "ok")
                    self.assertTrue((neighbor_result.get("probe") or {}).get("fake_transport"))
                    traces = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in trace_dir.glob("*.json")}
                    interrupted_trace = traces[str(signal_requests[0]["pid"])]
                    neighbor_trace = next(item for item in traces.values() if item["pid"] != signal_requests[0]["pid"])
                    self.assertEqual(interrupted_trace["override"], "0")
                    self.assertEqual(neighbor_trace["override"], inherited)
                    self.assertTrue(any(item in {"probe", "remote-probe"} for item in interrupted_trace["argv"]))
                    for options in mux_options(neighbor_trace):
                        self.assertEqual(options[0], "ControlMaster=auto")
                        self.assertTrue(options[1].startswith("ControlPath=" + str(mux_dir)))
                        self.assertEqual(options[2], "ControlPersist=120")
                    records.append({"inherited": inherited, "mode": mode, "signal_requests": signal_requests})
            self.assertEqual(len(records), 4)

            inprocess_code = (
                "import json, os, subprocess, sys\n"
                "from unittest import mock\n"
                f"sys.path.insert(0, {str(AGENTS)!r})\n"
                f"sys.path.insert(0, {str(LIB)!r})\n"
                "from maturation.invoke import RemoteDevInvoker, apply_real_execution_environment\n"
                "installed = apply_real_execution_environment()\n"
                "invoker = RemoteDevInvoker()\n"
                "invoker._dispatcher()\n"
                "import remote_dev.core.ssh_transport as transport\n"
                "import remote_dev.mcp.tools, remote_dev.core\n"
                "def fake_run(argv, **kwargs):\n"
                "    output = json.dumps({'status': 'ok', 'summary': {'hostname': 'fixture', 'python': '3.9.9'}, 'fake_transport': True})\n"
                "    if kwargs.get('text'):\n"
                "        return subprocess.CompletedProcess(list(argv), 0, output, '')\n"
                "    return subprocess.CompletedProcess(list(argv), 0, output.encode(), b'')\n"
                "with mock.patch.object(transport.subprocess, 'run', fake_run):\n"
                "    payload = invoker.call('remote.probe', "
                + repr(probe_args)
                + ")\n"
                "print(json.dumps({'installed': str(installed), 'tools': remote_dev.mcp.tools.__file__, 'core': remote_dev.core.__file__, "
                "'status': payload['result']['status'], 'fake': payload['result'].get('probe', {}).get('fake_transport')}))\n"
            )
            env = {
                **os.environ,
                "REMOTE_DEV_STATE_DIR": str(state / "inprocess"),
                "REMOTE_DEV_SSH_MUX_DIR": str(root / "inprocess-mux"),
                "REMOTE_DEV_RUNTIME_ENV_FILE": "/etc/profile.d/vaws-ascend-env.sh",
            }
            proc = subprocess.run([sys.executable, "-c", inprocess_code], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            inprocess = json.loads(proc.stdout.strip().splitlines()[-1])
            pinned = str(self.provider.resolve())
            self.assertEqual(inprocess["installed"], pinned)
            self.assertTrue(inprocess["tools"].startswith(pinned), inprocess["tools"])
            self.assertTrue(inprocess["core"].startswith(pinned), inprocess["core"])
            self.assertEqual(inprocess["status"], "ok")
            self.assertTrue(inprocess["fake"])
        self.assertEqual(dict(os.environ), original_env)


class SubstratePackageTests(unittest.TestCase):
    """Integration: skips when vaws-remote-dev is not installed."""

    def setUp(self) -> None:
        self.provider = require_optional_provider()

    def test_s3_substrate_integration_against_installed_package(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "test_remote_dev_consumer.SubstrateIntegrationTests"],
            cwd=str(AGENTS / "tests"),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Ran 3 tests", proc.stderr)
        self.assertNotIn("skipped", proc.stderr.lower())


class ProviderPackageTests(unittest.TestCase):
    """Installed package is the provider; no checkout locator remains."""

    def test_optional_and_pinned_agree_on_the_installed_package(self) -> None:
        optional = require_optional_provider()
        pinned = require_pinned_provider()
        self.assertEqual(optional, pinned)
        self.assertTrue((optional / "mcp" / "server.py").is_file())
        self.assertTrue((optional / "core" / "endpoint.py").is_file())

    def test_missing_package_skips(self) -> None:
        with mock.patch("maturation_provider.inspect", return_value={"state": "missing"}):
            with mock.patch("importlib.util.find_spec", return_value=None):
                with self.assertRaises(unittest.SkipTest) as ctx:
                    require_pinned_provider()
        self.assertIn("uv sync", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
