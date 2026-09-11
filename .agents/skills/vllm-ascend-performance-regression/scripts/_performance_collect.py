"""Business A/B scheduling over the coordinator's source and service ownership."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[4]
for directory in (ROOT / ".agents/lib", ROOT / ".agents/skills/vllm-ascend-benchmark/scripts",
                  ROOT / ".agents/skills/vllm-ascend-serving/scripts", Path(__file__).parent):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from _benchmark_common import (assemble_config, call_serve_start, extract_metrics, run_bench_on_remote,
    fixed_dataset_bench_args, prepare_fixed_request_dataset)
from _serving_start import wait_for_ready
from vaws_remote_target import ssh_endpoint_from_mapping
from vaws_report import report_directory
from vaws_task_target import PENDING, task_client


def collect_measurement(client, benchmark, sources, entry, *, context_file, service, warmups, startup_timeout, fixed_dataset=None):
    from performance_regression import PerformanceRegressionError, emit_progress
    client.sources(sources)
    config = assemble_config(context_file=context_file, service=service, **benchmark)
    execution_id = None
    try:
        reply = call_serve_start(config)
        execution_id = reply.get("execution_id")
        if not execution_id:
            raise PerformanceRegressionError(f"service submission returned no execution: {reply}")
        config.execution_id = execution_id
        if reply.get("state") in PENDING:
            emit_progress("wait-for-runtime", execution_id=execution_id)
            reply = client.wait(execution_id, until="running", timeout_seconds=startup_timeout, poll_interval=3)
        target = client.target(execution_id)
        if not target.get("live") or not target.get("service_port"):
            raise PerformanceRegressionError(f"service did not reach running: {reply}")
        endpoint = ssh_endpoint_from_mapping(target["endpoint"])
        port = int(target["service_port"])
        served_model = config.served_model_name or Path(config.model).name
        ready = wait_for_ready(endpoint, port, config.health_timeout or 300, served_model,
            still_running=lambda: client.observe(execution_id).get("state") == "running",
            log_text=lambda: str(client.observe(execution_id, "tail").get("tail") or ""))
        if not ready.get("ready"):
            raise PerformanceRegressionError(f"service failed business readiness: {ready}")
        url = f"http://{endpoint.host}:{port}"
        dataset = None
        if fixed_dataset:
            dataset = prepare_fixed_request_dataset(endpoint.host, endpoint.port,
                model=config.model, tokenizer_mode=fixed_dataset.get("tokenizer_mode", "auto"),
                input_len=fixed_dataset["input_len"], output_len=fixed_dataset["output_len"],
                path=f"/tmp/{service}-requests.jsonl", num_rows=fixed_dataset.get("num_rows", 64),
                prompt=fixed_dataset.get("prompt"), python=target["python"],
                env_preamble=target.get("launch_preamble", "") + "\n")
            config.bench_args = fixed_dataset_bench_args(config.bench_args,
                dataset_path=dataset["dataset_path"], output_len=fixed_dataset["output_len"])
        # Each state is prepared independently. Warm every new launch before
        # a measured sample, including BA transitions later in the schedule.
        warmup_results = []
        for _ in range(warmups if entry["phase"] == "measure" else 0):
            warmup_results.append(run_bench_on_remote(config, url, served_model, endpoint.host, endpoint.port))
        raw = run_bench_on_remote(config, url, served_model, endpoint.host, endpoint.port)
        if raw.get("failed", 0):
            raise PerformanceRegressionError("benchmark contains failed requests")
        if dataset:
            raw.setdefault("observation", {})["dataset"] = "sha256:" + dataset["dataset_sha256"]
            raw["dataset_preparation"] = dataset
        return {"status": "ok", "state": entry["state"], "phase": entry["phase"], "ordinal": entry["ordinal"],
                "metrics": extract_metrics(raw), "observation": raw.get("observation", {}),
                "raw_result": raw, "warmup_results": warmup_results, "execution_id": execution_id}
    finally:
        if not execution_id:
            execution_id = client.resolve_execution(service=service)
        if execution_id:
            client.observe(execution_id, "stop")
            stopped = client.wait(execution_id, until="released", timeout_seconds=60, poll_interval=2)
            if stopped.get("resources_released") is not True:
                client.observe(execution_id, "stop", force=True)
                stopped = client.wait(execution_id, until="released", timeout_seconds=30, poll_interval=2)
            if stopped.get("resources_released") is not True:
                raise PerformanceRegressionError(f"cleanup incomplete for owned execution {execution_id}: {stopped}")


def collect_experiment(config_path: Path, *, output_dir=None, context_file=None):
    from performance_regression import PerformanceRegressionError, build_report, build_schedule, emit_progress
    config = json.loads(config_path.read_text(encoding="utf-8"))
    benchmark = config.get("benchmark")
    if not isinstance(benchmark, dict) or not benchmark.get("model"):
        raise PerformanceRegressionError("benchmark.model is required")
    for state in ("baseline", "candidate"):
        sources = config.get(state, {}).get("sources")
        if not isinstance(sources, dict) or not {"vllm", "vllm-ascend"} <= set(sources):
            raise PerformanceRegressionError(f"{state}.sources must bind the vllm and vllm-ascend worktrees")
    runs, warmups = config.get("runs", 3), config.get("warmups", 1)
    if type(runs) is not int or runs < 2 or type(warmups) is not int or warmups < 1:
        raise PerformanceRegressionError("runs must be at least 2 and warmups at least 1")
    if not config.get("thresholds"):
        raise PerformanceRegressionError("metric-specific thresholds are required")
    client = task_client(context_file)
    original = {name: row["path"] for name, row in client.context["session"].get("sources", {}).items()}
    output = report_directory(ROOT, "performance-collection", output_dir)
    if output.exists() and any(output.iterdir()):
        raise PerformanceRegressionError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    failure = None
    service = "performance-" + uuid4().hex[:12]
    try:
        for entry in build_schedule(warmups=warmups, runs=runs):
            emit_progress("collect", state=entry["state"], measurement_phase=entry["phase"], ordinal=entry["ordinal"])
            result = collect_measurement(client, benchmark, config[entry["state"]]["sources"], entry,
                context_file=context_file, service=service, warmups=warmups,
                startup_timeout=config.get("startup_timeout_seconds", 1800), fixed_dataset=config.get("fixed_dataset"))
            path = output / f"{entry['index']:03d}-{entry['id']}.json"
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            paths.append(path)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if original:
            try:
                client.sources(original)
            except Exception as exc:
                failure = f"{failure or ''}; source restoration: {exc}".strip("; ")
    report = build_report(config_path, paths, output_dir=output / "report", collection_error=failure)
    if failure:
        report["collection_error"] = failure
    return report
