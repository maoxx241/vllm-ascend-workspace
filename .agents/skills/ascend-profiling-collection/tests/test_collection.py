#!/usr/bin/env python3
"""Hermetic tests for ascend-profiling-collection tunnel, bracket, and manifest logic.

No network, no NPU, no developer HOME. Tunnel argv is checked with the real
``ssh -G`` parser (fail if ``ssh`` is missing). Orchestration uses injected
collaborators.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".agents" / "skills" / "ascend-profiling-collection" / "scripts"
LIB = ROOT / ".agents" / "lib"
for path in (str(SCRIPTS), str(LIB)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _load(name: str, filename: str):
    sys.modules.pop(name, None)
    if name == "_common":
        sys.modules.pop("_common", None)
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # profile_control / collect call ensure_workspace_interpreter at import.
    # Scope the skip to this exec so collection cannot disable the hop
    # for the rest of the pytest process.
    with mock.patch.dict(os.environ, {"VAWS_SKIP_VENV_REEXEC": "1"}):
        spec.loader.exec_module(module)
    return module


# Collection ``_common`` must win over any other skill's module of the same name
# while siblings load. Drop the generic alias afterwards so a later skill
# suite in the same pytest process can import its own ``_common``.
sys.modules.pop("_common", None)
common = _load("vaws_profcoll_common_under_test", "_common.py")
sys.modules["_common"] = common
profile_control = _load("vaws_profcoll_profile_control_under_test", "profile_control.py")
collect = _load("vaws_profcoll_collect_under_test", "collect_torch_profile_case.py")
if sys.modules.get("_common") is common:
    del sys.modules["_common"]
# collect / profile_control insert this skill's scripts/ onto sys.path.
# Leave it there and later suites import the wrong ``_common``.
_scripts = str(SCRIPTS)
while _scripts in sys.path:
    sys.path.remove(_scripts)


class FakeProcess:
    def __init__(self, returncode: int | None = None, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = io.StringIO(stderr)
        self.terminated = False
        self.killed = False
        self.pid = 1_000_001

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def fake_endpoint() -> SimpleNamespace:
    return SimpleNamespace(host="192.0.2.10", port=46001, user="root")


def fake_target(*, alias: str = "machine-a", session_id: str = "sess-a") -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        session_file=None,
        alias=alias,
        endpoint=fake_endpoint(),
        state_repo_root=ROOT,
    )


def collect_argv(tmp: str, **overrides: object) -> list[str]:
    values: dict[str, object] = {
        "--session-id": "sess-a",
        "--model": "/models/Qwen",
        "--served-model-name": "Qwen",
        "--tp": "1",
        "--tag": "p16-tunnel",
        "--mode": "enforce_eager",
        "--request-kind": "text",
        "--benchmark-output-tokens": "8",
        "--benchmark-total-requests": "1",
        "--benchmark-concurrency": "1",
        "--followup-output-tokens": "2",
        "--prompt-tokens": "4",
    }
    values.update({k: str(v) for k, v in overrides.items()})
    argv: list[str] = []
    for key, value in values.items():
        argv.extend([key, str(value)])
    return argv


def _parse_ssh_g(text: str) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(" ")
        parsed.setdefault(key.lower(), []).append(value)
    return parsed


def _effective_ssh_config(cmd: list[str], home: str) -> tuple[dict[str, list[str]], str]:
    ssh = shutil.which("ssh")
    if ssh is None:
        raise AssertionError(
            "ssh binary is required to parse tunnel argv; a skipped parser "
            "test is how a dead tunnel survives"
        )
    result = subprocess.run(
        [ssh, "-G", "-F", "/dev/null", *cmd[1:]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"HOME": home, "PATH": os.environ.get("PATH", "")},
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"ssh -G failed (rc={result.returncode}): {(result.stderr or '')[:2000]}"
        )
    return _parse_ssh_g(result.stdout), result.stdout


class TunnelArgvTests(unittest.TestCase):
    """Assert the ``open_local_forward`` path, not a skill-built argv.

    If collection goes back to ``subprocess.Popen`` + ``ssh_argv``,
    ``test_open_local_tunnel_uses_unmuxed_keepalive_forward`` fails because
    the package Popen is never called. If that package argv loses
    ``ExitOnForwardFailure`` or puts ``-N``/``-L`` after ``--``, the real
    ``ssh -G`` parse fails the same way the pre-migration hand-rolled
    command did.
    """

    def test_open_local_tunnel_uses_unmuxed_keepalive_forward(self) -> None:
        import remote_dev.core.ssh_transport as ssh_transport

        captured: dict[str, list[str]] = {}

        def fake_popen(cmd, **_kwargs):
            captured["cmd"] = list(cmd)
            return FakeProcess()

        connect_sock = mock.MagicMock()
        connect_sock.__enter__.return_value = connect_sock
        connect_sock.connect.return_value = None

        with (
            mock.patch.object(common.subprocess, "Popen") as skill_popen,
            mock.patch.object(ssh_transport.subprocess, "Popen", side_effect=fake_popen),
            mock.patch.object(ssh_transport.socket, "socket", return_value=connect_sock),
            mock.patch.object(ssh_transport, "_find_free_local_port", return_value=34567),
            mock.patch.object(ssh_transport.os, "killpg", side_effect=ProcessLookupError),
        ):
            with common.open_local_tunnel(fake_endpoint(), 8000) as tunnel:
                self.assertEqual(tunnel["local_port"], 34567)
                self.assertEqual(tunnel["base_url"], "http://127.0.0.1:34567")

        skill_popen.assert_not_called()
        cmd = captured["cmd"]
        self.assertEqual(cmd[0], "ssh")
        sep = cmd.index("--")
        self.assertEqual(cmd[sep + 1 :], ["192.0.2.10"])
        for token in ("ExitOnForwardFailure=yes", "-N", "-L"):
            self.assertLess(cmd.index(token), sep, token)

        with tempfile.TemporaryDirectory() as home:
            cfg, raw = _effective_ssh_config(cmd, home)
        self.assertEqual(cfg.get("exitonforwardfailure"), ["yes"])
        forwards = [item.replace("[", "").replace("]", "") for item in cfg.get("localforward") or []]
        self.assertTrue(
            any("127.0.0.1:34567" in item and "127.0.0.1:8000" in item for item in forwards),
            raw,
        )
        self.assertEqual(cfg.get("sessiontype"), ["none"])
        self.assertEqual(cfg.get("controlmaster"), ["false"])
        self.assertEqual(cfg.get("serveraliveinterval"), ["30"])
        self.assertEqual(cfg.get("serveralivecountmax"), ["10"])

    def test_open_local_tunnel_raises_when_ssh_exits_before_listen(self) -> None:
        import remote_dev.core.ssh_transport as ssh_transport

        def fake_popen(cmd, **_kwargs):
            return FakeProcess(returncode=255, stderr="bind: Address already in use")

        with mock.patch.object(ssh_transport.subprocess, "Popen", side_effect=fake_popen):
            with self.assertRaisesRegex(RuntimeError, r"exited early"):
                with common.open_local_tunnel(fake_endpoint(), 8000):
                    self.fail("context must not yield after the tunnel dies")


class ProfileControlTests(unittest.TestCase):
    def test_post_remote_action_rejects_unknown_action(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported action"):
            profile_control.post_remote_action(fake_endpoint(), 8000, "pause_profile", 10)

    def test_post_remote_action_posts_start_and_stop_paths(self) -> None:
        seen: list[str] = []

        def fake_ssh(_ep, script, check=False):
            seen.append(script)
            return SimpleNamespace(returncode=0, stdout='{"ok": true, "status": 200, "body": ""}', stderr="")

        with mock.patch.object(profile_control, "ssh_exec", side_effect=fake_ssh):
            start = profile_control.post_remote_action(fake_endpoint(), 9001, "start_profile", 30)
            stop = profile_control.post_remote_action(fake_endpoint(), 9001, "stop_profile", 30)
        self.assertTrue(start["ok"])
        self.assertTrue(stop["ok"])
        self.assertIn("http://127.0.0.1:9001/start_profile", seen[0])
        self.assertIn("http://127.0.0.1:9001/stop_profile", seen[1])


def _ok_request(index: int = 0) -> collect.RequestResult:
    return collect.RequestResult(
        index=index, ok=True, status=200, latency_sec=0.01, body={"id": "x"}, error=None
    )


@contextlib.contextmanager
def _fake_tunnel(_ep, _port):
    yield {"local_port": 39999, "base_url": "http://127.0.0.1:39999"}


def _patch_collection(run_dir: Path, **overrides: object):
    patches = {
        "resolve_execution_target": mock.Mock(return_value=fake_target()),
        "unique_collection_run_dir": mock.Mock(return_value=run_dir),
        "knowledge_preflight_advisories": mock.Mock(return_value=[]),
        "knowledge_failure_matches": mock.Mock(return_value=[]),
        "call_serve_start": mock.Mock(
            return_value={"status": "ready", "runtime_dir": "/tmp/runtime", "port": 8000}
        ),
        "open_local_tunnel": _fake_tunnel,
        "post_remote_action": mock.Mock(
            return_value={"ok": True, "status": 200, "body": ""}
        ),
        "_run_benchmark_wave": mock.Mock(return_value=[_ok_request(0)]),
        "_send_chat_request": mock.Mock(return_value=_ok_request(1)),
        "call_serve_stop": mock.Mock(return_value={"status": "stopped"}),
        "analyse_profile_root": mock.Mock(
            return_value={
                "dirs": [{"path": "/tmp/runtime/vllm_profile/rank0_ascend_pt", "outputs": {}}],
                "rank_count": 1,
                "analysis_status": "ok",
                "expected_output_kind": "db",
                "analyse_wall_s": 0.1,
                "analyse_parallelism": 1,
            }
        ),
    }
    patches.update(overrides)
    return mock.patch.multiple(collect, **patches)


class CollectionOrchestrationTests(unittest.TestCase):
    def test_start_stop_bracket_writes_ok_manifest(self) -> None:
        order: list[str] = []

        def record_action(_ep, _port, action, _timeout):
            order.append(action)
            return {"ok": True, "status": 200, "body": action}

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            with (
                mock.patch.object(collect.time, "sleep"),
                _patch_collection(
                    run_dir,
                    post_remote_action=mock.Mock(side_effect=record_action),
                ),
            ):
                rc = collect.main(collect_argv(tmp))

            self.assertEqual(rc, 0)
            self.assertEqual(order, ["start_profile", "stop_profile"])
            manifest_path = run_dir / "manifest.json"
            self.assertTrue(manifest_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "ok")
            self.assertEqual(manifest["start_profile"]["body"], "start_profile")
            self.assertEqual(manifest["stop_profile"]["body"], "stop_profile")
            self.assertEqual(manifest["request_tunnel"]["base_url"], "http://127.0.0.1:39999")
            self.assertEqual(manifest["workload_status"]["status"], "ok")
            self.assertEqual(manifest["analysis_status"], "ok")

    def test_tunnel_death_writes_failed_manifest_and_never_returns_zero(self) -> None:
        @contextlib.contextmanager
        def dead_tunnel(_ep, _port):
            raise RuntimeError("ssh tunnel exited early (rc=255): broken pipe")
            yield  # pragma: no cover

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            with (
                mock.patch.object(collect.time, "sleep"),
                _patch_collection(run_dir, open_local_tunnel=dead_tunnel),
            ):
                rc = collect.main(collect_argv(tmp))

            self.assertNotEqual(rc, 0)
            self.assertEqual(rc, 1)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertIn("ssh tunnel exited early", manifest["error"]["message"])
            self.assertNotEqual(manifest.get("status"), "ok")

    def test_unusable_workload_fails_after_writing_manifest(self) -> None:
        failed = collect.RequestResult(
            index=0, ok=False, status=500, latency_sec=0.01, body=None, error="boom"
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            with (
                mock.patch.object(collect.time, "sleep"),
                _patch_collection(
                    run_dir,
                    _run_benchmark_wave=mock.Mock(return_value=[failed]),
                    _send_chat_request=mock.Mock(return_value=failed),
                ),
            ):
                rc = collect.main(collect_argv(tmp))

            self.assertEqual(rc, 1)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["workload_status"]["status"], "followup_failed")


class WorkloadGateTests(unittest.TestCase):
    def test_evaluate_workload_requires_followup_and_threshold(self) -> None:
        ok = [_ok_request(0), _ok_request(1)]
        self.assertEqual(
            collect._evaluate_workload(ok, _ok_request(2), 0.8)["status"],
            "ok",
        )
        self.assertEqual(
            collect._evaluate_workload(ok, None, 0.8)["status"],
            "followup_failed",
        )
        self.assertEqual(
            collect._evaluate_workload([], _ok_request(0), 0.8)["status"],
            "no_benchmark_requests",
        )
        mixed = [
            _ok_request(0),
            collect.RequestResult(1, False, 500, 0.1, None, "x"),
        ]
        self.assertEqual(
            collect._evaluate_workload(mixed, _ok_request(2), 0.8)["status"],
            "benchmark_below_threshold",
        )


class CommonImportIsolationTests(unittest.TestCase):
    def test_generic_common_alias_is_not_left_in_sys_modules(self) -> None:
        cached = sys.modules.get("_common")
        self.assertIsNot(
            cached,
            common,
            "collection tests must not leave their helper as sys.modules['_common']; "
            "that poisons later skill suites in the same pytest process",
        )


if __name__ == "__main__":
    unittest.main()
