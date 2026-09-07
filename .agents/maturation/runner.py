"""Orchestrate one maturation run across endpoints.

Endpoint lifecycle: health probe → scratch setup → operations → scratch
teardown. Endpoints that fail the probe or setup are *skipped and reported*,
never repaired. Everything remote lives under one identifiable scratch path
that is removed at the end unless the caller asks to keep it.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import shlex
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .evidence import EvidenceStore
from .invoke import Invoker
from .knowledge import build_candidates, capture_candidate
from .redact import Redactor
from .scenarios import EndpointContext, Executor, result_text, run_operation, utc_now_iso
from .spec import Operation, OperationSet
from .stats import aggregate

PROGRESS_SENTINEL = "__VAWS_MATURATION_PROGRESS__="
REMOTE_SCRATCH_PREFIX = "/tmp/vaws-maturation"
SEED_FILES = 8


def emit_progress(phase: str, message: str, extra: Mapping[str, Any] | None = None) -> None:
    import json

    payload = {"phase": phase, "message": message, **(extra or {})}
    sys.stderr.write(PROGRESS_SENTINEL + json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stderr.flush()


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"mat-{stamp}-{uuid.uuid4().hex[:6]}"


class RunConfig:
    def __init__(
        self,
        *,
        repetitions: int | None = None,
        operation_filter: set[str] | None = None,
        shape_filter: set[str] | None = None,
        class_filter: set[str] | None = None,
        endpoint_parallelism: int = 2,
        keep_remote_scratch: bool = False,
        min_free_mib: int = 512,
        max_load_1m: float | None = None,
        capture_knowledge: bool = False,
        capture_extra_args: list[str] | None = None,
        min_reproductions: int = 2,
        reveal_hosts: bool = False,
    ) -> None:
        self.repetitions = repetitions
        self.operation_filter = operation_filter
        self.shape_filter = shape_filter
        self.class_filter = class_filter
        self.endpoint_parallelism = max(1, endpoint_parallelism)
        self.keep_remote_scratch = keep_remote_scratch
        self.min_free_mib = min_free_mib
        self.max_load_1m = max_load_1m
        self.capture_knowledge = capture_knowledge
        self.capture_extra_args = capture_extra_args or []
        self.min_reproductions = min_reproductions
        self.reveal_hosts = reveal_hosts


def select_operations(operation_set: OperationSet, config: RunConfig, endpoint_kind: str) -> list[Operation]:
    selected: list[Operation] = []
    for op in operation_set.enabled():
        if not op.applies_to(endpoint_kind):
            continue
        if config.operation_filter and op.id not in config.operation_filter:
            continue
        if config.shape_filter and op.shape not in config.shape_filter:
            continue
        if config.class_filter and op.op_class not in config.class_filter:
            continue
        selected.append(op)
    return selected


def _health_command(scratch: str, min_free_mib: int) -> str:
    return "\n".join(
        [
            "set -u",
            f"mkdir -p {shlex.quote(scratch)}/seed || exit 3",
            f"free_kib=$(df -Pk {shlex.quote(scratch)} | awk 'NR==2{{print $4}}')",
            "load=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || echo 0)",
            "ncpu=$(nproc 2>/dev/null || echo 1)",
            "hostname_ok=1; getent hosts \"$(hostname)\" >/dev/null 2>&1 || hostname_ok=0",
            f"cd {shlex.quote(scratch)}/seed || exit 3",
            f"for i in $(seq 1 {SEED_FILES}); do printf 'seed line alpha-%s\\nneedle-token\\n' \"$i\" > \"seed-$i.txt\"; done",
            "printf 'HEALTH free_kib=%s load1=%s ncpu=%s hostname_ok=%s python=%s\\n' \"$free_kib\" \"$load\" \"$ncpu\" \"$hostname_ok\" \"$(python3 -c 'import platform;print(platform.python_version())' 2>/dev/null || echo none)\"",
            f"if [ \"${{free_kib:-0}}\" -lt {min_free_mib * 1024} ]; then echo LOW_DISK; exit 4; fi",
            "echo ready",
        ]
    )


def _parse_health(text: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for line in text.splitlines():
        if line.startswith("HEALTH "):
            for token in line[len("HEALTH ") :].split():
                if "=" in token:
                    key, value = token.split("=", 1)
                    facts[key] = value
    out: dict[str, Any] = {}
    if "free_kib" in facts and facts["free_kib"].isdigit():
        out["free_mib"] = int(facts["free_kib"]) // 1024
    try:
        out["load_1m"] = float(facts.get("load1", "nan"))
    except ValueError:
        pass
    if facts.get("ncpu", "").isdigit():
        out["ncpu"] = int(facts["ncpu"])
    out["hostname_mapped"] = facts.get("hostname_ok") == "1"
    out["python"] = facts.get("python")
    return out


def prepare_endpoint(
    endpoint: EndpointContext,
    invoker: Invoker,
    config: RunConfig,
) -> dict[str, Any]:
    """Probe + scratch setup. Returns a status dict; ``ok`` False means skip."""
    executor = Executor(invoker, endpoint)
    probe = executor.call(
        "probe",
        "remote.probe",
        {},
        {"outcome": "success", "status": "ok"},
        timeout_ms=30000,
        root="/",
        cwd="/tmp",
    )
    if not probe.passed:
        return {
            "ok": False,
            "reason": "probe_failed",
            "detail": probe.expectation_error or probe.exception or "probe failed",
            "raw": probe.to_record()["raw"],
        }
    summary = ((probe.result or {}).get("probe") or {}).get("summary") or {}
    hostname = str(summary.get("hostname") or "")
    endpoint.environment.update(
        {
            "python": summary.get("python"),
            "hostname_sha256": hashlib.sha256(hostname.encode("utf-8")).hexdigest()[:16] if hostname else None,
            "probe_duration_ms": probe.duration_ms,
        }
    )
    setup = executor.call(
        "setup",
        "remote.bash",
        {"command": _health_command(endpoint.scratch, config.min_free_mib)},
        {"outcome": "success", "status": "ok", "stdout_contains": "ready"},
        timeout_ms=60000,
        root="/",
        cwd="/tmp",
    )
    health = _parse_health(result_text(setup.payload, setup.result))
    endpoint.environment.update(health)
    if not setup.passed:
        text = result_text(setup.payload, setup.result)
        reason = "low_disk" if "LOW_DISK" in text else "setup_failed"
        return {"ok": False, "reason": reason, "detail": setup.expectation_error or setup.exception or "setup failed", "health": health, "raw": setup.to_record()["raw"]}
    if config.max_load_1m is not None and health.get("load_1m") is not None and health["load_1m"] > config.max_load_1m:
        teardown_endpoint(endpoint, invoker)
        return {"ok": False, "reason": "host_busy", "detail": f"load1={health['load_1m']} exceeds --max-load {config.max_load_1m}", "health": health}
    return {"ok": True, "health": health, "probe_ms": probe.duration_ms, "setup_ms": setup.duration_ms}


def teardown_endpoint(endpoint: EndpointContext, invoker: Invoker) -> dict[str, Any]:
    executor = Executor(invoker, endpoint)
    scratch = shlex.quote(endpoint.scratch)
    step = executor.call(
        "teardown",
        "remote.bash",
        {
            "command": (
                f"rm -rf {scratch} && if [ -e {scratch} ]; then echo STILL_PRESENT; exit 5; fi; "
                f"rmdir -p {shlex.quote(str(PurePosixPath(endpoint.scratch).parent))} 2>/dev/null || true; echo gone"
            )
        },
        {"outcome": "success", "status": "ok", "stdout_contains": "gone"},
        timeout_ms=60000,
        root="/",
        cwd="/tmp",
    )
    return {"ok": step.passed, "detail": step.expectation_error or step.exception, "duration_ms": step.duration_ms}


def run_endpoint(
    endpoint: EndpointContext,
    operations: list[Operation],
    invoker: Invoker,
    config: RunConfig,
    store: EvidenceStore,
    local_tmp: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    emit_progress("endpoint", f"preparing {endpoint.label} ({endpoint.kind})", {"endpoint": endpoint.label})
    prep = prepare_endpoint(endpoint, invoker, config)
    record: dict[str, Any] = {
        "label": endpoint.label,
        "kind": endpoint.kind,
        "environment": dict(endpoint.environment),
        "prepare": prep,
        "skipped": not prep.get("ok"),
        "operations_run": 0,
        "trials": 0,
    }
    if not prep.get("ok"):
        emit_progress("endpoint", f"skipping {endpoint.label}: {prep.get('reason')}", {"endpoint": endpoint.label, "reason": prep.get("reason")})
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        return record
    trials_total = 0
    try:
        for op in operations:
            trials = run_operation(
                op,
                endpoint,
                invoker,
                repetitions=config.repetitions,
                local_tmp=local_tmp,
                progress=emit_progress,
            )
            for trial in trials:
                store.append_trial(trial)
            trials_total += len(trials)
            record["operations_run"] += 1
    finally:
        if config.keep_remote_scratch:
            record["teardown"] = {"ok": None, "detail": "kept by request", "scratch": endpoint.scratch}
        else:
            record["teardown"] = teardown_endpoint(endpoint, invoker)
    record["trials"] = trials_total
    record["duration_ms"] = int((time.monotonic() - started) * 1000)
    return record


def run(
    *,
    operation_set: OperationSet,
    endpoints: list[Mapping[str, Any]],
    invoker: Invoker,
    config: RunConfig,
    evidence_root: Path,
    run_id: str | None = None,
    local_tmp: Path | None = None,
) -> dict[str, Any]:
    run_id = run_id or new_run_id()
    started_at = utc_now_iso()
    start = time.monotonic()
    store = EvidenceStore(evidence_root / run_id)
    store.write_hosts(list(endpoints))
    local_tmp = local_tmp or (store.run_dir / "tmp")
    local_tmp.mkdir(parents=True, exist_ok=True)

    contexts: list[EndpointContext] = []
    for item in endpoints:
        contexts.append(
            EndpointContext(
                label=str(item["label"]),
                kind=str(item.get("kind", "host")),
                host=str(item["host"]),
                port=int(item["port"]),
                user=str(item.get("user") or "root"),
                # Per-endpoint scratch: a container may bind-mount the host's
                # /tmp, so a host and its container must never share a path.
                scratch=f"{REMOTE_SCRATCH_PREFIX}/{run_id}/{item['label']}",
                run_id=run_id,
                connect_timeout_ms=int(item.get("connect_timeout_ms") or 10000),
                runtime_env=bool(item.get("runtime_env", False)),
            )
        )

    plan = {
        ctx.label: [op.id for op in select_operations(operation_set, config, ctx.kind)] for ctx in contexts
    }
    emit_progress("plan", f"run {run_id}: {len(contexts)} endpoint(s)", {"run_id": run_id, "operations": {k: len(v) for k, v in plan.items()}})

    endpoint_records: list[dict[str, Any]] = []
    lock = threading.Lock()

    def worker(ctx: EndpointContext) -> dict[str, Any]:
        ops = select_operations(operation_set, config, ctx.kind)
        record = run_endpoint(ctx, ops, invoker, config, store, local_tmp)
        with lock:
            endpoint_records.append(record)
        return record

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.endpoint_parallelism) as pool:
        list(pool.map(worker, contexts))
    endpoint_records.sort(key=lambda item: str(item["label"]))

    return finalize(
        store=store,
        operation_set=operation_set,
        endpoints=endpoints,
        endpoint_records=endpoint_records,
        plan=plan,
        config=config,
        run_id=run_id,
        started_at=started_at,
        duration_ms=int((time.monotonic() - start) * 1000),
    )


def finalize(
    *,
    store: EvidenceStore,
    operation_set: OperationSet,
    endpoints: list[Mapping[str, Any]],
    endpoint_records: list[dict[str, Any]],
    plan: Mapping[str, Any],
    config: RunConfig,
    run_id: str,
    started_at: str,
    duration_ms: int,
) -> dict[str, Any]:
    """Aggregate trials, prepare/capture knowledge candidates, write the report.

    Shared by a live run and by ``--report <run-id>`` replays so knowledge
    capture can happen after the fact from retained evidence.
    """
    redactor = Redactor.for_endpoints(endpoints)
    trials = store.load_trials()
    summary = aggregate(trials, operation_set.classes)
    failures = [
        {
            "trial_id": t["trial_id"],
            "operation_id": t["operation_id"],
            "endpoint_label": t["endpoint_label"],
            "endpoint_kind": t["endpoint_kind"],
            "attempt": t["attempt"],
            "duration_ms": t["duration_ms"],
            "outcome": t.get("outcome"),
            "status": t.get("status"),
            "attribution": t.get("attribution"),
            "command": t.get("command"),
            "raw_tail": (t.get("raw") or {}).get("text_tail", "")[-400:],
        }
        for t in trials
        if not t.get("passed")
    ]

    candidates = build_candidates(
        trials,
        run_id=run_id,
        evidence_relative_dir=store.relative_dir,
        redactor=redactor,
        min_reproductions=config.min_reproductions,
    )
    knowledge: dict[str, Any] = {"candidates_prepared": [], "captured": [], "capture_requested": config.capture_knowledge}
    for candidate in candidates:
        # Give the candidate a stable id for the evidence file name; the capture
        # path recomputes/validates its own id from the semantic fields.
        preview_id = hashlib.sha256(
            (candidate["summary"] + "|" + "|".join(candidate["fingerprints"])).encode("utf-8")
        ).hexdigest()[:12]
        store.write_candidate({**candidate, "candidate_id": f"maturation-{preview_id}"})
        knowledge["candidates_prepared"].append({"summary": candidate["summary"], "fingerprints": candidate["fingerprints"], "confidence": candidate["confidence"]})
        if config.capture_knowledge:
            emit_progress("knowledge", f"capturing candidate: {candidate['summary']}")
            knowledge["captured"].append({"summary": candidate["summary"], **capture_candidate(candidate, extra_args=config.capture_extra_args)})

    report = {
        "schema_version": "vaws.maturation-report.v1",
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "duration_ms": duration_ms,
        "operations_source": _relative(operation_set.source),
        "config": {
            "repetitions_override": config.repetitions,
            "operation_filter": sorted(config.operation_filter) if config.operation_filter else None,
            "shape_filter": sorted(config.shape_filter) if config.shape_filter else None,
            "class_filter": sorted(config.class_filter) if config.class_filter else None,
            "endpoint_parallelism": config.endpoint_parallelism,
            "keep_remote_scratch": config.keep_remote_scratch,
            "remote_scratch": f"{REMOTE_SCRATCH_PREFIX}/{run_id}/<endpoint-label>",
        },
        "endpoints": endpoint_records,
        "plan": dict(plan),
        "summary": summary,
        "failures": failures,
        "knowledge": knowledge,
        "evidence_dir": store.relative_dir,
        "status": "ok" if all(not r.get("skipped") for r in endpoint_records) and summary["totals"]["failures"] == 0 else ("partial" if trials else "failed"),
    }
    store.write_run({**report, "hosts": list(endpoints)})
    public = report if config.reveal_hosts else redactor.value(report)
    store.write_summary(render_markdown(public))
    return public


def replay(
    *,
    operation_set: OperationSet,
    config: RunConfig,
    evidence_root: Path,
    run_id: str,
) -> dict[str, Any]:
    """Rebuild the report (and optionally capture knowledge) from retained evidence."""
    import json

    run_dir = evidence_root / run_id
    run_path = run_dir / "run.json"
    if not run_path.exists():
        raise FileNotFoundError(f"no retained run.json for {run_id}")
    previous = json.loads(run_path.read_text(encoding="utf-8"))
    store = EvidenceStore(run_dir)
    return finalize(
        store=store,
        operation_set=operation_set,
        endpoints=list(previous.get("hosts") or []),
        endpoint_records=list(previous.get("endpoints") or []),
        plan=previous.get("plan") or {},
        config=config,
        run_id=run_id,
        started_at=str(previous.get("started_at") or ""),
        duration_ms=int(previous.get("duration_ms") or 0),
    )


def _relative(path: str) -> str:
    from .evidence import REPO_ROOT

    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except (ValueError, OSError):
        return Path(path).name


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report.get("summary", {})
    totals = summary.get("totals", {})
    lines = [
        f"# Maturation run `{report.get('run_id')}`",
        "",
        f"- started: {report.get('started_at')}  finished: {report.get('finished_at')}  ({(report.get('duration_ms') or 0) // 1000}s)",
        f"- trials: {totals.get('trials')}  passes: {totals.get('passes')}  failures: {totals.get('failures')}  pass rate: {_pct(totals.get('pass_rate'))}",
        f"- failure layers: {totals.get('failure_layers') or '{}'}",
        "",
        "## Endpoints",
        "",
        "| label | kind | status | remote python | free MiB | load1 | hostname mapped | trials |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for ep in report.get("endpoints", []):
        env = ep.get("environment", {})
        status = "skipped: " + str((ep.get("prepare") or {}).get("reason")) if ep.get("skipped") else "ran"
        lines.append(
            f"| {ep.get('label')} | {ep.get('kind')} | {status} | {env.get('python')} | {env.get('free_mib')} | {env.get('load_1m')} | {env.get('hostname_mapped')} | {ep.get('trials')} |"
        )
    lines += [
        "",
        "## Operations",
        "",
        "| operation | class | shape | n | pass | rate | 95% lower | p50 ms | p95 ms | verdict | failure layers |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for op_id, item in summary.get("operations", {}).items():
        d = item.get("durations", {})
        lines.append(
            f"| `{op_id}` | {item.get('class')} | {item.get('shape')} | {item.get('n')} | {item.get('passes')} | {_pct(item.get('pass_rate'))} | {_pct(item.get('pass_rate_lower_95'))} | {d.get('median_ms')} | {d.get('p95_ms')} | **{item.get('verdict')}** | {item.get('failure_layers') or ''} |"
        )
    lines += ["", "## Least mature first", ""]
    for index, item in enumerate(summary.get("ranking", [])[:15], start=1):
        lines.append(f"{index}. `{item['operation_id']}` — {item['verdict']} ({_pct(item.get('pass_rate'))}, n={item.get('n')}) {item.get('failure_layers') or ''}")
    failures = report.get("failures", [])
    lines += ["", f"## Failures ({len(failures)})", ""]
    for item in failures[:60]:
        attribution = item.get("attribution") or {}
        lines.append(
            f"- `{item['trial_id']}` [{attribution.get('layer')}] step={attribution.get('step')} status={item.get('status')} {item.get('duration_ms')}ms — {attribution.get('reason')}"
        )
    if len(failures) > 60:
        lines.append(f"- … {len(failures) - 60} more in the evidence directory")
    knowledge = report.get("knowledge", {})
    lines += ["", "## Knowledge candidates", ""]
    prepared = knowledge.get("candidates_prepared", [])
    if not prepared:
        lines.append("- none (no failure reproduced at least the configured minimum number of times)")
    for item in prepared:
        lines.append(f"- {item['summary']} (confidence {item['confidence']})")
    for item in knowledge.get("captured", []):
        lines.append(f"  - captured: status={item.get('status')} action={item.get('action')} id={item.get('candidate_id')}")
    lines += ["", f"Evidence: `{report.get('evidence_dir')}` (untracked)", ""]
    return "\n".join(lines)


def _pct(value: Any) -> str:  # noqa: ANN401
    if not isinstance(value, (int, float)):
        return "-"
    return f"{value * 100:.1f}%"


__all__ = [
    "PROGRESS_SENTINEL",
    "REMOTE_SCRATCH_PREFIX",
    "RunConfig",
    "emit_progress",
    "new_run_id",
    "prepare_endpoint",
    "finalize",
    "render_markdown",
    "replay",
    "run",
    "run_endpoint",
    "select_operations",
    "teardown_endpoint",
]
