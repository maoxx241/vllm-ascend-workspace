#!/usr/bin/env python3
"""Hermetic tests for ascend-profiling-collection tunnel, bracket, and manifest logic.

No network, no NPU, no developer HOME. Tunnel ownership is checked at the
remote-dev public API boundary. Orchestration uses injected collaborators.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
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


class ImageEncodingTests(unittest.TestCase):
    def test_image_encoding_handles_unicode_path_and_preserves_source(self):
        import base64
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "鍥剧墖 source.png"
            Image.new("RGBA", (8, 4), (200, 30, 20, 128)).save(path)
            before = path.read_bytes()
            url, metadata = collect._build_image_data_url(path, 12)
            with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as encoded:
                self.assertEqual(encoded.size, (24, 12))
                self.assertEqual(encoded.mode, "RGB")
            self.assertEqual(metadata["source_width"], 8)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(temp).iterdir()), [path])

    def test_nonpositive_height_is_rejected_before_reading_image(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            collect._build_image_data_url(Path("missing.png"), 0)


class AnalyseEvidenceTests(unittest.TestCase):
    def test_both_export_requires_nonempty_database(self):
        analyse = sys.modules["run_remote_analyse"]
        with tempfile.TemporaryDirectory() as temp:
            rank = Path(temp) / "rank_ascend_pt"
            output = rank / analyse.ASCEND_OUTPUT_DIRNAME
            output.mkdir(parents=True)
            (output / "kernel_details.csv").write_text("header\n", encoding="utf-8")
            (output / "trace_view.json").write_text("{}", encoding="utf-8")
            self.assertEqual(analyse.classify_status(analyse.verify_outputs_local(rank, "text")), "ok")
            self.assertEqual(analyse.classify_status(analyse.verify_outputs_local(rank, "both")), "partial")
            (output / "ascend_pytorch_profiler_0_1.db").write_bytes(b"data")
            self.assertEqual(analyse.classify_status(analyse.verify_outputs_local(rank, "both")), "ok")

    def test_analyse_uses_managed_interpreter_and_preamble(self):
        analyse = sys.modules["run_remote_analyse"]
        script = analyse.build_parallel_analyse_script(["/trace/rank_ascend_pt"], parallelism=1, timeout_s=20,
            python="/managed env/bin/python", preamble="export MANAGED_ENV=1")
        self.assertIn("'/managed env/bin/python' -c", script)
        self.assertIn("export MANAGED_ENV=1", script)


def fake_endpoint():
    return common.SshEndpoint(host="192.0.2.10", port=46001, user="root")


def fake_target(*, alias: str = "machine-a", session_id: str = "sess-a"):
    return common.ExecutionTarget(
        mode="endpoint",
        alias=alias,
        endpoint=fake_endpoint(),
        cwd="/vllm-workspace",
        session_id=session_id,
        task_id=session_id,
        service_port=8000,
    )


def collect_argv(tmp: str, **overrides: object) -> list[str]:
    values: dict[str, object] = {
        "--service": "vllm",
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


class TunnelOwnershipTests(unittest.TestCase):
    """The skill consumes one package-owned forward and releases its context."""

    class ForwardError(Exception):
        pass

    def test_open_local_tunnel_delegates_endpoint_and_closes_forward(self) -> None:
        endpoint = fake_endpoint()
        lifecycle = []

        @contextlib.contextmanager
        def forward(_endpoint, _port):
            lifecycle.append("enter")
            try:
                yield SimpleNamespace(local_port=34567, local_host="127.0.0.1")
            finally:
                lifecycle.append("exit")

        with (
            mock.patch.object(common, "require_transport", return_value={"RemoteExecutionError": self.ForwardError}),
            mock.patch.object(common, "open_local_forward", side_effect=forward) as open_forward,
        ):
            with common.open_local_tunnel(endpoint, 8000) as tunnel:
                self.assertEqual(tunnel["local_port"], 34567)
                self.assertEqual(tunnel["base_url"], "http://127.0.0.1:34567")
                self.assertEqual(lifecycle, ["enter"])
            open_forward.assert_called_once_with(endpoint, 8000)
        self.assertEqual(lifecycle, ["enter", "exit"])

    def test_open_local_tunnel_translates_package_startup_failure(self) -> None:
        failure = self.ForwardError("forward exited early")
        with (
            mock.patch.object(common, "require_transport", return_value={"RemoteExecutionError": self.ForwardError}),
            mock.patch.object(common, "open_local_forward", side_effect=failure),
        ):
            with self.assertRaisesRegex(RuntimeError, "exited early") as raised:
                with common.open_local_tunnel(fake_endpoint(), 8000):
                    self.fail("context must not yield after the tunnel dies")
        self.assertIs(raised.exception.__cause__, failure)


class ProfileControlTests(unittest.TestCase):
    def test_post_remote_action_rejects_unknown_action(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported action"):
            profile_control.post_remote_action(fake_endpoint(), 8000, "pause_profile", 10)

    def test_missing_or_failed_http_payload_cannot_report_success(self) -> None:
        for stdout in ("", "not-json", "[]", '{"ok":false,"status":500}', '{"ok":true,"status":false}'):
            with self.subTest(stdout=stdout), mock.patch.object(profile_control, "ssh_exec", return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr="")):
                with self.assertRaisesRegex(RuntimeError, "no confirmed HTTP success"):
                    profile_control.post_remote_action(fake_endpoint(), 8000, "start_profile", 10)

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

    def test_workload_failure_closes_existing_profile_window_without_stopping_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            controls, stop_service = mock.Mock(return_value={"ok": True, "status": 200}), mock.Mock()
            with _patch_collection(run_dir, post_remote_action=controls, call_serve_stop=stop_service,
                                   _run_benchmark_wave=mock.Mock(side_effect=RuntimeError("workload interrupted"))):
                rc = collect.main(collect_argv(tmp, **{"--execution-id": "live-owned-execution"}))
            self.assertEqual(rc, 1)
            self.assertEqual([call.args[2] for call in controls.call_args_list], ["start_profile", "stop_profile"])
            stop_service.assert_not_called()
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["task_id"], "sess-a")
            self.assertEqual(manifest["service_result"]["status"], "existing")

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
