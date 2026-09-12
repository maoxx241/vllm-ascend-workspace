import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import _performance_collect as collector
import performance_regression as performance
from test_performance_regression import config, recorded_observation


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_one_call_uses_collected_observations_without_agent_metadata(tmp_path):
    settings = config()
    settings = {key: settings[key] for key in ("runs", "warmups", "thresholds")}
    paths = []
    for entry in performance.build_schedule(warmups=1, runs=3):
        observation = recorded_observation(commit="abc" if entry["state"] == "baseline" else "def")
        paths.append(write(tmp_path / f"{entry['index']}.json", {**entry, "status": "ok",
            "metrics": {"output_throughput": 100, "mean_ttft_ms": 10}, "observation": observation}))
    result = performance.build_report(write(tmp_path / "input.json", settings), paths, output_dir=tmp_path / "report")
    assert result["status"] == "passed"
    assert json.loads(Path(result["manifest"]).read_text())["status"] == "passed"


def test_missing_observation_finishes_an_inconclusive_report(tmp_path):
    settings = {"runs": 2, "warmups": 1, "thresholds": config()["thresholds"]}
    sample = write(tmp_path / "result.json", {"state": "baseline", "phase": "measure", "ordinal": 1,
                                           "status": "ok", "metrics": {"output_throughput": 100}})
    result = performance.build_report(write(tmp_path / "input.json", settings), [sample], output_dir=tmp_path / "report")
    assert result["status"] == "inconclusive"
    assert Path(result["report"]).is_file()


def test_source_comparison_derives_code_variables_but_retains_workload_confounder(tmp_path):
    settings = {"runs": 2, "warmups": 1, "thresholds": config()["thresholds"],
                "baseline": {"sources": {"vllm": "/src/base/vllm", "vllm-ascend": "/src/base/ascend"}},
                "candidate": {"sources": {"vllm": "/src/next/vllm", "vllm-ascend": "/src/next/ascend"}}}
    paths = []
    for entry in performance.build_schedule(warmups=1, runs=2):
        observation = recorded_observation()
        observation["workspace_snapshot"]["vllm_commit"] = entry["state"]
        observation["native_digest"] = {"build_key": entry["state"]}
        paths.append(write(tmp_path / f"{entry['index']}.json", {**entry, "status": "ok",
            "metrics": {"output_throughput": 100, "mean_ttft_ms": 10}, "observation": observation}))
    config_path = write(tmp_path / "input.json", settings)
    assert performance.build_report(config_path, paths, output_dir=tmp_path / "good")["status"] == "passed"
    for path in paths:
        row = json.loads(path.read_text())
        if row["state"] == "candidate":
            row["observation"]["engine_args"] = {"enforce_eager": True}
            write(path, row)
    report = performance.build_report(config_path, paths, output_dir=tmp_path / "confounded")
    assert report["status"] == "inconclusive"
    comparison = json.loads((tmp_path / "confounded" / "comparison.json").read_text())
    assert comparison["observed_status"] == "passed"
    assert comparison["metrics"]["throughput"]["baseline"]["mean"] == 100
    assert comparison["comparability"]["verdict"] == "not-comparable"


def test_normalization_keeps_runtime_observation():
    observation = recorded_observation()
    result = performance.normalize_benchmark_result({"status": "ok", "metrics": {"output_throughput": 100},
        "observation": observation}, state="baseline", phase="measure", ordinal=1, shared={}, source="result.json")
    assert result["observation"] == observation


def test_collector_runs_abba_without_rebinding_task_sources(tmp_path):
    client = Mock(context={"session": {"sources": {"vllm": {"path": "original-vllm"}, "vllm-ascend": {"path": "original-ascend"}}}})
    settings = {"benchmark": {"model": "/models/example"}, "runs": 2, "warmups": 1,
                "thresholds": config()["thresholds"],
                "baseline": {"sources": {"vllm": "base-vllm", "vllm-ascend": "base-ascend"}},
                "candidate": {"sources": {"vllm": "next-vllm", "vllm-ascend": "next-ascend"}}}
    seen = []
    def sample(_client, benchmark, sources, entry, **kwargs):
        seen.append((entry["phase"], entry["state"], sources))
        return {**entry, "status": "ok", "metrics": {"output_throughput": 100, "mean_ttft_ms": 10},
                "observation": recorded_observation(commit="abc" if entry["state"] == "baseline" else "def")}
    with patch.object(collector, "task_client", return_value=client), patch.object(collector, "collect_measurement", side_effect=sample):
        result = collector.collect_experiment(write(tmp_path / "input.json", settings), output_dir=tmp_path / "collection", context_file="native-context")
    assert [state for phase, state, _ in seen if phase == "measure"] == ["baseline", "candidate", "candidate", "baseline"]
    assert all(sources == settings[state]["sources"] for _, state, sources in seen)
    client.sources.assert_not_called()
    assert result["status"] == "passed"


@pytest.mark.parametrize("source_mode", ["automatic", "explicit-empty", "explicit"])
def test_experiment_preserves_native_and_explicit_source_defaults(tmp_path, source_mode):
    from vaws_coordinator.agent_session import AgentSessions, source_defaults
    from vaws_coordinator.task_client import TaskClient

    def repository(name):
        path = tmp_path / name
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture", "--allow-empty"], check=True)
        return str(path)

    native, baseline, candidate = map(repository, ("native", "baseline", "candidate"))
    store = AgentSessions(tmp_path / "sessions")
    context = store.bind_native_sources(store.attach("codex", "native-thread", native))
    if source_mode != "automatic":
        context = store.bind_sources(context, {} if source_mode == "explicit-empty" else {"custom": native})
    before = source_defaults(context["session"], context["attachment"])
    client = TaskClient(context["context_file"], user="fixture", service=Mock())
    client.target = Mock(return_value={"live": True, "service_port": 8000, "python": "/managed/python",
                                      "endpoint": {"host": "example.invalid", "port": 22}})
    client.observe = Mock()
    client.wait = Mock(return_value={"state": "succeeded", "resources_released": True})
    settings = {"benchmark": {"model": "/models/example"}, "runs": 2, "warmups": 1,
                "thresholds": config()["thresholds"],
                "baseline": {"sources": {"vllm": baseline, "vllm-ascend": baseline}},
                "candidate": {"sources": {"vllm": candidate, "vllm-ascend": candidate}}}
    submitted = []

    def submit(_config, *, sources=None):
        current = store.context(context["attachment"]["id"])
        effective = sources if sources is not None else {
            name: ref["path"] for name, ref in source_defaults(current["session"], current["attachment"])["sources"].items()
        }
        submitted.append(effective)
        return {"execution_id": f"owned-{len(submitted)}", "state": "running"}

    raw = {"output_throughput": 100, "mean_ttft_ms": 10, "observation": recorded_observation()}
    with patch.object(collector, "task_client", return_value=client), \
         patch.object(collector, "call_serve_start", side_effect=submit), \
         patch.object(collector, "wait_for_ready", return_value={"ready": True}), \
         patch.object(collector, "run_bench_on_remote", return_value=raw):
        collector.collect_experiment(write(tmp_path / "input.json", settings), output_dir=tmp_path / "collection",
                                     context_file=context["context_file"])

    after = store.context(context["attachment"]["id"])
    assert source_defaults(after["session"], after["attachment"]) == before
    assert submitted == [settings[entry["state"]]["sources"] for entry in performance.build_schedule(warmups=1, runs=2)]


def test_benchmark_failure_stops_only_created_execution():
    client = Mock()
    client.target.return_value = {"live": True, "service_port": 8000, "python": "/managed/python",
                                  "endpoint": {"host": "example.invalid", "port": 22}}
    client.wait.return_value = {"state": "cancelled", "resources_released": True}
    cfg = SimpleNamespace(model="/models/example", served_model_name="example", health_timeout=1, execution_id=None)
    with patch.object(collector, "assemble_config", return_value=cfg), patch.object(collector, "call_serve_start", return_value={"execution_id": "owned", "state": "running"}), \
         patch.object(collector, "wait_for_ready", return_value={"ready": True}), patch.object(collector, "run_bench_on_remote", side_effect=RuntimeError("workload failed")):
        with pytest.raises(RuntimeError, match="workload failed"):
            collector.collect_measurement(client, {}, {"vllm": "a"}, {"phase": "measure", "state": "baseline", "ordinal": 1},
                context_file="native-context", service="unique-owned-service", warmups=1, startup_timeout=1)
    client.observe.assert_called_once_with("owned", "stop")
    assert client.wait.call_args.kwargs["until"] == "released"


def test_submission_timeout_resolves_owned_service_for_cleanup():
    client = Mock()
    client.resolve_execution.return_value = "submitted-before-timeout"
    client.wait.return_value = {"state": "cancelled", "resources_released": True}
    with patch.object(collector, "assemble_config", return_value=object()), patch.object(collector, "call_serve_start", side_effect=TimeoutError("lost reply")):
        with pytest.raises(TimeoutError):
            collector.collect_measurement(client, {}, {"vllm": "a"}, {}, context_file="native-context",
                service="unique-owned-service", warmups=1, startup_timeout=1)
    client.resolve_execution.assert_called_once_with(service="unique-owned-service")
    client.observe.assert_called_once_with("submitted-before-timeout", "stop")

# This suite exercises report semantics, not coordinator Git snapshotting.
# Real code-identity tests belong to the coordinator package.
from unittest.mock import patch as _patch_report_code
_REPORT_CODE = {"source_head": "1" * 40, "snapshot_commit": "2" * 40, "dirty": True}
_report_code_patch = _patch_report_code("vaws_coordinator.code_identity.manifest_code", return_value=_REPORT_CODE)


def setup_module():
    _report_code_patch.start()


def teardown_module():
    _report_code_patch.stop()


setUpModule = setup_module
tearDownModule = teardown_module
