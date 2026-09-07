#!/usr/bin/env python3
"""Joint consumer/provider routing tests with a fake transport and no host.

The four-case mux fixture is the accepted #93 independent joint, with provider
discovery through the scaffold locator and the tracked pin. The second joint
goes through ``RemoteDevInvoker.call_cli`` and the real scaffold launcher into
the pinned ``tools`` wrapper.
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

import vaws_remote_dev as remote_dev  # noqa: E402
from maturation.invoke import LAUNCHER, RemoteDevInvoker, run_cli  # noqa: E402
from maturation_provider import require_pinned_provider  # noqa: E402

MUX_CHILD = AGENTS / "tests" / "maturation_mux_child.py"
ROOT_ENV = remote_dev.REMOTE_DEV_ROOT_ENV
MUX_VAR = "REMOTE_DEV_SSH_MUX"

SITECUSTOMIZE = r'''
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_original_run = subprocess.run
_is_launcher = Path(sys.argv[0]).name == "remote_dev.py"
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
        "root": os.environ.get("VAWS_REMOTE_DEV_ROOT"),
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


def _write_required_files(root: Path) -> None:
    for relative in remote_dev.REQUIRED_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_git_checkout(root: Path) -> str:
    _git(root, "init")
    _git(root, "config", "user.email", "maturation-test@example.com")
    _git(root, "config", "user.name", "maturation-test")
    _git(root, "add", "-A")
    _git(root, "commit", "--no-verify", "-m", "test checkout")
    commit = remote_dev.checkout_commit(root)
    if not commit:
        raise AssertionError(f"failed to read HEAD for test checkout {root}")
    return commit


def _must_fail_not_skip() -> Any:
    try:
        require_pinned_provider()
    except unittest.SkipTest as exc:
        raise AssertionError("explicit invalid checkout must not skip") from exc
    except AssertionError:
        raise
    else:
        raise AssertionError("expected explicit invalid checkout to fail")


class FourCaseMuxJointTests(unittest.TestCase):
    """Accepted #93 joint fixture; provider comes from the scaffold locator."""

    def setUp(self) -> None:
        self.provider = require_pinned_provider()

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
                    argv = [sys.executable, str(MUX_CHILD), str(self.provider)]

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
    """``call_cli`` through the real launcher into the pinned tools wrapper."""

    def setUp(self) -> None:
        self.provider = require_pinned_provider()

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
                        self.assertTrue(any(Path(item).name.startswith("remote_") and Path(item).suffix == ".py" for item in trace["argv"]))
                        signal_requests.append({"pid": pid, "mode": current_mode, "mocked": True})
                        current_gate.write_text("release test-owned fixture")
                        return []

                    child_env = {
                        ROOT_ENV: str(self.provider),
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
                    self.assertEqual(launched_argv[0][1], str(LAUNCHER))
                    self.assertEqual(launched_argv[0][2:6], ["tool", "remote_probe", "--input-json", "-"])
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
                    self.assertEqual(interrupted_trace["root"], str(self.provider))
                    self.assertEqual(neighbor_trace["root"], str(self.provider))
                    self.assertTrue(any(Path(item).name == "remote_probe.py" for item in interrupted_trace["argv"]))
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
                "checkout = apply_real_execution_environment()\n"
                "invoker = RemoteDevInvoker()\n"
                "invoker._dispatcher()\n"
                "import core.ssh_transport as transport\n"
                "import mcp.tools, core\n"
                "def fake_run(argv, **kwargs):\n"
                "    output = json.dumps({'status': 'ok', 'summary': {'hostname': 'fixture', 'python': '3.9.9'}, 'fake_transport': True})\n"
                "    if kwargs.get('text'):\n"
                "        return subprocess.CompletedProcess(list(argv), 0, output, '')\n"
                "    return subprocess.CompletedProcess(list(argv), 0, output.encode(), b'')\n"
                "with mock.patch.object(transport.subprocess, 'run', fake_run):\n"
                "    payload = invoker.call('remote.probe', "
                + repr(probe_args)
                + ")\n"
                "print(json.dumps({'checkout': str(checkout), 'tools': mcp.tools.__file__, 'core': core.__file__, "
                "'status': payload['result']['status'], 'fake': payload['result'].get('probe', {}).get('fake_transport')}))\n"
            )
            env = {
                **os.environ,
                ROOT_ENV: str(self.provider),
                "REMOTE_DEV_STATE_DIR": str(state / "inprocess"),
                "REMOTE_DEV_SSH_MUX_DIR": str(root / "inprocess-mux"),
                "REMOTE_DEV_RUNTIME_ENV_FILE": "/etc/profile.d/vaws-ascend-env.sh",
            }
            proc = subprocess.run([sys.executable, "-c", inprocess_code], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            inprocess = json.loads(proc.stdout.strip().splitlines()[-1])
            pinned = str(self.provider.resolve())
            self.assertEqual(inprocess["checkout"], pinned)
            self.assertTrue(inprocess["tools"].startswith(pinned), inprocess["tools"])
            self.assertTrue(inprocess["core"].startswith(pinned), inprocess["core"])
            self.assertEqual(inprocess["status"], "ok")
            self.assertTrue(inprocess["fake"])
        self.assertEqual(dict(os.environ), original_env)


class SubstratePinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = require_pinned_provider()

    def test_s3_substrate_integration_against_pinned_source(self) -> None:
        env = {**os.environ, ROOT_ENV: str(self.provider)}
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "test_remote_dev_consumer.SubstrateIntegrationTests"],
            cwd=str(AGENTS / "tests"),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Ran 3 tests", proc.stderr)
        self.assertNotIn("skipped", proc.stderr.lower())


class ProviderLocatorTests(unittest.TestCase):
    """Focused locator/pin coverage; no hardcoded checkout alias."""

    def test_configured_alternate_path_uses_locator(self) -> None:
        provider = require_pinned_provider()
        pin = remote_dev.load_dependency()["commit"]
        with tempfile.TemporaryDirectory() as tmp:
            alt = Path(tmp) / "alternate-checkout"
            alt.symlink_to(provider)
            with mock.patch.dict(os.environ, {ROOT_ENV: str(alt)}, clear=False):
                found = require_pinned_provider()
                status = remote_dev.checkout_status()
            self.assertEqual(status["root_source"], "env")
            self.assertEqual(status["root"], str(alt))
            self.assertEqual(remote_dev.checkout_commit(found), pin)
            self.assertTrue(remote_dev.looks_like_checkout(alt))

    def test_missing_unconfigured_provider_skips_like_optional_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent"
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(ROOT_ENV, None)
                with mock.patch.object(remote_dev, "default_checkout_dir", return_value=missing):
                    with self.assertRaises(unittest.SkipTest) as ctx:
                        require_pinned_provider()
        self.assertIn(ROOT_ENV, str(ctx.exception))

    def test_explicit_missing_checkout_fails_without_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "absent")
            with mock.patch.dict(os.environ, {ROOT_ENV: missing}, clear=False):
                with self.assertRaises(AssertionError) as ctx:
                    _must_fail_not_skip()
        self.assertIn("not a remote-dev checkout", str(ctx.exception))

    def test_explicit_malformed_checkout_fails_without_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            malformed = Path(tmp) / "partial"
            malformed.mkdir()
            (malformed / "mcp").mkdir()
            (malformed / "mcp" / "server.py").write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {ROOT_ENV: str(malformed)}, clear=False):
                with self.assertRaises(AssertionError) as ctx:
                    _must_fail_not_skip()
        self.assertIn("not a remote-dev checkout", str(ctx.exception))

    def test_explicit_wrong_pin_fails_without_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "checkout"
            fake.mkdir()
            _write_required_files(fake)
            _init_git_checkout(fake)
            with mock.patch.dict(os.environ, {ROOT_ENV: str(fake)}, clear=False):
                with self.assertRaises(AssertionError) as ctx:
                    _must_fail_not_skip()
        self.assertIn("does not match tracked pin", str(ctx.exception))

    def test_explicit_dirty_checkout_fails_without_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "checkout"
            fake.mkdir()
            _write_required_files(fake)
            sha = _init_git_checkout(fake)
            (fake / "dirty.txt").write_text("uncommitted", encoding="utf-8")
            with mock.patch.object(remote_dev, "load_dependency", return_value={"commit": sha}):
                with mock.patch.dict(os.environ, {ROOT_ENV: str(fake)}, clear=False):
                    with self.assertRaises(AssertionError) as ctx:
                        _must_fail_not_skip()
        self.assertIn("is not clean", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
