"""Adversarial shapes: how one declared operation is exercised per repetition.

Every shape produces one trial record per repetition. A trial carries the
exact command, the environment identity, the raw tool output, the timing,
and — on failure — a layer attribution. Composite shapes record their
sub-steps so a failure can be located inside the sequence.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import shlex
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .attribution import attribute
from .invoke import CliResult, Invoker
from .spec import Operation, render

ProgressFn = Callable[[str, str, dict[str, Any]], None]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


@dataclass
class EndpointContext:
    label: str
    kind: str
    host: str
    port: int
    user: str
    scratch: str
    run_id: str
    connect_timeout_ms: int = 10000
    runtime_env: bool = False
    environment: dict[str, Any] = field(default_factory=dict)

    def payload(self, *, root: str | None = None, cwd: str | None = None) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "root": root or self.scratch,
            "cwd": cwd or root or self.scratch,
            "connect_timeout_ms": self.connect_timeout_ms,
            "runtime_env": self.runtime_env,
        }

    def template_context(self, attempt: int, worker: int | None = None) -> dict[str, Any]:
        return {
            "scratch": self.scratch,
            "trial": attempt,
            "worker": worker if worker is not None else 0,
            "label": self.label,
            "run_id": self.run_id,
        }

    def identity(self) -> dict[str, Any]:
        return {
            "endpoint_label": self.label,
            "endpoint_kind": self.kind,
            "remote_python": self.environment.get("python"),
            "remote_hostname_sha256": self.environment.get("hostname_sha256"),
        }


# --------------------------------------------------------------------------- #
# Expectation evaluation
# --------------------------------------------------------------------------- #


def _texts(value: Any) -> list[str]:  # noqa: ANN401
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        out: list[str] = []
        for item in value.values():
            out.extend(_texts(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_texts(item))
        return out
    return []


def result_text(payload: Mapping[str, Any] | None, result: Mapping[str, Any] | None) -> str:
    parts: list[str] = []
    if isinstance(payload, Mapping) and isinstance(payload.get("text"), str):
        parts.append(payload["text"])
    if isinstance(result, Mapping):
        parts.extend(_texts(result.get("preview")))
    return "\n".join(parts)


def _as_list(value: Any) -> list[Any]:  # noqa: ANN401
    return value if isinstance(value, list) else [value]


def evaluate(result: Mapping[str, Any] | None, expect: Mapping[str, Any], text: str) -> str | None:
    """Return an error description when ``result`` violates ``expect``."""
    if not isinstance(result, Mapping):
        return "no result payload"
    if "outcome" in expect and result.get("outcome") not in _as_list(expect["outcome"]):
        return f"outcome {result.get('outcome')!r} not in {_as_list(expect['outcome'])!r}"
    if "status" in expect and result.get("status") not in _as_list(expect["status"]):
        return f"status {result.get('status')!r} not in {_as_list(expect['status'])!r}"
    for needle in _as_list(expect.get("stdout_contains", [])):
        if needle and str(needle) not in text:
            return f"expected text {needle!r} not found in tool output"
    for needle in _as_list(expect.get("stdout_excludes", [])):
        if needle and str(needle) in text:
            return f"forbidden text {needle!r} found in tool output"
    if "min_duration_ms" in expect:
        duration = result.get("duration_ms")
        if not isinstance(duration, (int, float)) or duration < int(expect["min_duration_ms"]):
            return f"duration {duration} ms below minimum {expect['min_duration_ms']} ms"
    if "exit_code" in expect and result.get("exit_code") != expect["exit_code"]:
        return f"exit code {result.get('exit_code')!r} != {expect['exit_code']!r}"
    if "file_count" in expect:
        manifest = result.get("manifest") if isinstance(result.get("manifest"), Mapping) else {}
        if manifest.get("file_count") != int(expect["file_count"]):
            return f"manifest file_count {manifest.get('file_count')!r} != {expect['file_count']}"
    return None


# --------------------------------------------------------------------------- #
# Trial records
# --------------------------------------------------------------------------- #


def _raw_from_result(payload: Mapping[str, Any] | None, result: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"text_tail": (payload or {}).get("text", "")[-2000:] if isinstance(payload, Mapping) else ""}
    text = result_text(payload, result)
    return {
        "outcome": result.get("outcome"),
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "timed_out": result.get("timed_out"),
        "tool_duration_ms": result.get("duration_ms"),
        "invocation_id": result.get("invocation_id"),
        "refs": result.get("refs") or {},
        "warnings": result.get("warnings") or [],
        "error": result.get("error"),
        "text_tail": text[-2000:],
    }


def _describe_call(tool: str, args: Mapping[str, Any]) -> str:
    """Exact command text for evidence, endpoint fields removed."""
    hidden = {"host", "port", "user", "connect_timeout_ms", "runtime_env"}
    shown = {key: value for key, value in args.items() if key not in hidden}
    if tool == "remote.bash" and "command" in shown:
        return f"{tool}: {shown['command']}"
    rendered = ", ".join(f"{key}={_short(value)}" for key, value in sorted(shown.items()))
    return f"{tool}({rendered})"


def _short(value: Any) -> str:  # noqa: ANN401
    text = repr(value)
    return text if len(text) <= 160 else text[:157] + "..."


@dataclass
class Step:
    name: str
    tool: str
    command: str
    started_at: str
    duration_ms: int
    passed: bool
    result: dict[str, Any] | None
    payload: dict[str, Any] | None
    expectation_error: str | None = None
    exception: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    timeout_ms: int | None = None

    def to_record(self) -> dict[str, Any]:
        record = {
            "name": self.name,
            "tool": self.tool,
            "command": self.command,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "passed": self.passed,
            "expectation_error": self.expectation_error,
            "exception": self.exception,
            "raw": _raw_from_result(self.payload, self.result),
        }
        if self.extra:
            record["extra"] = self.extra
        return record


class Executor:
    """Runs tool calls and turns them into ``Step`` records."""

    def __init__(self, invoker: Invoker, endpoint: EndpointContext) -> None:
        self.invoker = invoker
        self.endpoint = endpoint

    def call(
        self,
        name: str,
        tool: str,
        args: Mapping[str, Any],
        expect: Mapping[str, Any],
        *,
        timeout_ms: int,
        via: str = "inprocess",
        root: str | None = None,
        cwd: str | None = None,
        kill_after_ms: int | None = None,
        kill_mode: str = "wrapper",
        expect_killed: bool = False,
    ) -> Step:
        call_args = {**self.endpoint.payload(root=root, cwd=cwd), **args}
        call_args.setdefault("timeout_ms", timeout_ms)
        started_at = utc_now_iso()
        start = time.monotonic()
        payload: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        exception: str | None = None
        extra: dict[str, Any] = {"via": via}
        try:
            if via == "cli":
                cli: CliResult = self.invoker.call_cli(
                    tool,
                    call_args,
                    kill_after_ms=kill_after_ms,
                    kill_mode=kill_mode,
                    timeout_s=timeout_ms / 1000 + 60,
                )
                payload = cli.payload
                result = cli.result
                extra.update(
                    {
                        "returncode": cli.returncode,
                        "killed": cli.killed,
                        "kill_mode": kill_mode if kill_after_ms is not None else None,
                        "stderr_tail": cli.stderr_tail[-1500:],
                        "stdout_tail": "" if payload is not None else cli.stdout_tail[-1500:],
                    }
                )
            else:
                payload = self.invoker.call(tool, call_args)
                result = payload.get("result") if isinstance(payload, Mapping) else None
        except Exception as exc:  # noqa: BLE001
            exception = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
        duration_ms = _duration_ms(start)
        expectation_error: str | None
        if exception:
            expectation_error = None
            passed = False
        elif expect_killed:
            killed = bool(extra.get("killed"))
            expectation_error = None if killed else "process finished before the interruption deadline"
            passed = killed
        else:
            expectation_error = evaluate(result, expect, result_text(payload, result))
            passed = expectation_error is None
        return Step(
            name=name,
            tool=tool,
            command=_describe_call(tool, call_args),
            started_at=started_at,
            duration_ms=duration_ms,
            passed=passed,
            result=dict(result) if isinstance(result, Mapping) else None,
            payload=dict(payload) if isinstance(payload, Mapping) else None,
            expectation_error=expectation_error,
            exception=exception,
            extra=extra,
            timeout_ms=int(call_args.get("timeout_ms") or timeout_ms),
        )


def _trial_from_steps(
    op: Operation,
    endpoint: EndpointContext,
    attempt: int,
    steps: list[Step],
    *,
    started_at: str,
    start: float,
    notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failed_step = next((step for step in steps if not step.passed), None)
    passed = failed_step is None and bool(steps)
    attribution = None
    if not passed:
        if failed_step is None:
            attribution = {"layer": "harness", "reason": "no steps executed", "fingerprint": "no steps executed"}
        else:
            attribution = attribute(
                failed_step.result,
                expectation_error=failed_step.expectation_error,
                duration_ms=failed_step.duration_ms,
                timeout_ms=failed_step.timeout_ms,
                exception=failed_step.exception,
                op_class=op.op_class,
            )
            attribution["step"] = failed_step.name
    primary = failed_step or (steps[-1] if steps else None)
    return {
        "trial_id": f"{endpoint.run_id}-{endpoint.label}-{op.id}-{attempt:03d}",
        "run_id": endpoint.run_id,
        "operation_id": op.id,
        "op_class": op.op_class,
        "shape": op.shape,
        "tool": op.tool or (primary.tool if primary else None),
        "endpoint_label": endpoint.label,
        "endpoint_kind": endpoint.kind,
        "attempt": attempt,
        "started_at": started_at,
        "duration_ms": _duration_ms(start),
        "passed": passed,
        "outcome": primary.result.get("outcome") if primary and primary.result else None,
        "status": primary.result.get("status") if primary and primary.result else None,
        "attribution": attribution,
        "command": primary.command if primary else None,
        "environment": endpoint.identity(),
        "raw": _raw_from_result(primary.payload, primary.result) if primary else {},
        "steps": [step.to_record() for step in steps],
        "notes": notes or {},
    }


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #


def run_operation(
    op: Operation,
    endpoint: EndpointContext,
    invoker: Invoker,
    *,
    repetitions: int | None = None,
    local_tmp: Path | None = None,
    progress: ProgressFn | None = None,
) -> list[dict[str, Any]]:
    reps = repetitions or op.repetitions
    executor = Executor(invoker, endpoint)
    shape = SHAPE_RUNNERS[op.shape]
    trials: list[dict[str, Any]] = []
    tmp_root = local_tmp or Path(tempfile.gettempdir())
    for attempt in range(reps):
        started_at = utc_now_iso()
        start = time.monotonic()
        try:
            steps, notes = shape(op, endpoint, executor, attempt, tmp_root)
        except Exception as exc:  # noqa: BLE001
            steps = [
                Step(
                    name="harness",
                    tool=op.tool or "",
                    command="",
                    started_at=started_at,
                    duration_ms=_duration_ms(start),
                    passed=False,
                    result=None,
                    payload=None,
                    exception=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}",
                )
            ]
            notes = {}
        trials.append(_trial_from_steps(op, endpoint, attempt, steps, started_at=started_at, start=start, notes=notes))
    if progress:
        passes = sum(1 for t in trials if t["passed"])
        progress("operation", f"{op.id} on {endpoint.label}: {passes}/{len(trials)} passed", {"operation": op.id, "endpoint": endpoint.label, "passes": passes, "n": len(trials)})
    return trials


def _shape_baseline(op: Operation, endpoint: EndpointContext, executor: Executor, attempt: int, tmp_root: Path) -> tuple[list[Step], dict[str, Any]]:
    ctx = endpoint.template_context(attempt)
    args = render(op.args, ctx)
    expect = render(op.expect, ctx)
    assert op.tool is not None
    step = executor.call("call", op.tool, args, expect, timeout_ms=op.timeout_ms, via=op.via)
    return [step], {}


def _shape_concurrent(op: Operation, endpoint: EndpointContext, executor: Executor, attempt: int, tmp_root: Path) -> tuple[list[Step], dict[str, Any]]:
    assert op.tool is not None
    workers = int(op.params.get("concurrency", 4))
    via = "cli" if op.shape == "multi_session" else op.via

    def worker(index: int) -> Step:
        ctx = endpoint.template_context(attempt, worker=index)
        step = executor.call(
            f"worker-{index}",
            op.tool,  # type: ignore[arg-type]
            render(op.args, ctx),
            render(op.expect, ctx),
            timeout_ms=op.timeout_ms,
            via=via,
        )
        return step

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        steps = list(pool.map(worker, range(workers)))
    return steps, {"concurrency": workers, "via": via}


def _shape_stream_under_load(op: Operation, endpoint: EndpointContext, executor: Executor, attempt: int, tmp_root: Path) -> tuple[list[Step], dict[str, Any]]:
    params = op.params
    ctx = endpoint.template_context(attempt)
    stream_command = render(str(params.get("stream_command", "for i in $(seq 1 20); do echo line-$i; sleep 0.5; done")), ctx)
    side_command = str(params.get("side_command", "printf side-{worker}-{trial}"))
    side_count = int(params.get("side_count", 8))
    side_concurrency = int(params.get("side_concurrency", 4))
    stream_expect = render(dict(params.get("stream_expect", {"outcome": "success", "status": "ok"})), ctx)
    side_expect_template = dict(params.get("side_expect", {"outcome": "success", "status": "ok", "stdout_contains": "side-{worker}-{trial}"}))
    stream_via = str(params.get("stream_via", "cli"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=side_concurrency + 1) as pool:
        stream_future = pool.submit(
            executor.call,
            "stream",
            "remote.bash",
            {"command": stream_command},
            stream_expect,
            timeout_ms=op.timeout_ms,
            via=stream_via,
        )
        time.sleep(float(params.get("side_delay_s", 0.5)))

        def side(index: int) -> Step:
            wctx = endpoint.template_context(attempt, worker=index)
            return executor.call(
                f"side-{index}",
                "remote.bash",
                {"command": render(side_command, wctx)},
                render(side_expect_template, wctx),
                timeout_ms=int(params.get("side_timeout_ms", 30000)),
            )

        side_steps = list(pool.map(side, range(side_count)))
        stream_step = stream_future.result()
    return [stream_step, *side_steps], {"side_count": side_count, "stream_via": stream_via}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_local_files(base: Path, count: int, size: int, seed: str) -> dict[str, str]:
    base.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for index in range(count):
        path = base / f"blob-{index:03d}.bin"
        # Deterministic pseudo-random content: reproducible and incompressible enough.
        material = hashlib.sha256(f"{seed}-{index}".encode()).digest()
        chunks = []
        produced = 0
        counter = 0
        while produced < size:
            block = hashlib.sha256(material + counter.to_bytes(8, "big")).digest()
            chunks.append(block)
            produced += len(block)
            counter += 1
        data = b"".join(chunks)[:size]
        path.write_bytes(data)
        hashes[path.name] = hashlib.sha256(data).hexdigest()
    return hashes


def _remote_make_files_command(remote_dir: str, count: int, size: int, seed: str) -> str:
    script = "\n".join(
        [
            "import hashlib, os, pathlib, sys",
            f"base = pathlib.Path({remote_dir!r})",
            "base.mkdir(parents=True, exist_ok=True)",
            f"for index in range({count}):",
            f"    material = hashlib.sha256(f'{seed}-{{index}}'.encode()).digest()",
            "    out = bytearray()",
            "    counter = 0",
            f"    while len(out) < {size}:",
            "        out += hashlib.sha256(material + counter.to_bytes(8, 'big')).digest()",
            "        counter += 1",
            f"    (base / f'blob-{{index:03d}}.bin').write_bytes(bytes(out[:{size}]))",
            "print('made', len(list(base.iterdir())))",
        ]
    )
    return f"rm -rf {shlex.quote(remote_dir)} && python3 - <<'PY'\n{script}\nPY"


def _manifest_hashes(result: Mapping[str, Any] | None) -> dict[str, str]:
    manifest = result.get("manifest") if isinstance(result, Mapping) else None
    if not isinstance(manifest, Mapping):
        return {}
    out: dict[str, str] = {}
    for item in manifest.get("files", []) or []:
        if isinstance(item, Mapping):
            out[str(item.get("relpath"))] = str(item.get("sha256"))
    return out


def _verify_step(name: str, ok: bool, detail: str, *, status: str) -> Step:
    now = utc_now_iso()
    return Step(
        name=name,
        tool="harness.verify",
        command=name,
        started_at=now,
        duration_ms=0,
        passed=ok,
        result={"outcome": "success" if ok else "failed", "status": "ok" if ok else status, "error": None if ok else detail},
        payload=None,
        expectation_error=None if ok else detail,
    )


def _shape_large_transfer(op: Operation, endpoint: EndpointContext, executor: Executor, attempt: int, tmp_root: Path) -> tuple[list[Step], dict[str, Any]]:
    params = op.params
    files = int(params.get("files", 8))
    size = int(params.get("bytes_per_file", 512 * 1024))
    remote_dir = f"{endpoint.scratch}/xfer-{op.id}-{attempt:03d}"
    steps: list[Step] = []
    with tempfile.TemporaryDirectory(prefix="vaws-maturation-", dir=str(tmp_root)) as tmp:
        src = Path(tmp) / "src"
        expected = _make_local_files(src, files, size, seed=f"{endpoint.run_id}-{op.id}-{attempt}")
        steps.append(
            executor.call(
                "push",
                "remote.artifact_push",
                {"local_path": str(src), "remote_path": remote_dir},
                {"outcome": "success", "status": "ok"},
                timeout_ms=op.timeout_ms,
            )
        )
        manifest_step = executor.call(
            "manifest",
            "remote.artifact_manifest",
            {"remote_path": remote_dir},
            {"outcome": "success", "status": "ok", "file_count": files},
            timeout_ms=op.timeout_ms,
        )
        steps.append(manifest_step)
        remote_hashes = _manifest_hashes(manifest_step.result)
        steps.append(
            _verify_step(
                "verify-remote-hashes",
                remote_hashes == expected,
                f"remote manifest hashes differ from local source ({len(remote_hashes)} vs {len(expected)} files)",
                status="hash_mismatch",
            )
        )
        dst = Path(tmp) / "dst"
        steps.append(
            executor.call(
                "pull",
                "remote.artifact_pull",
                {"remote_path": remote_dir, "local_dir": str(dst)},
                {"outcome": "success", "status": "ok"},
                timeout_ms=op.timeout_ms,
            )
        )
        pulled = {path.name: _sha256(path) for path in dst.glob("blob-*.bin")} if dst.exists() else {}
        steps.append(
            _verify_step(
                "verify-pulled-hashes",
                pulled == expected,
                f"pulled files differ from source ({len(pulled)} vs {len(expected)} files)",
                status="hash_mismatch",
            )
        )
    steps.append(
        executor.call(
            "cleanup",
            "remote.bash",
            {"command": f"rm -rf {shlex.quote(remote_dir)}"},
            {"outcome": "success", "status": "ok"},
            timeout_ms=30000,
        )
    )
    total_bytes = files * size
    push_ms = steps[0].duration_ms or 1
    pull_ms = steps[3].duration_ms or 1
    notes = {
        "files": files,
        "bytes_per_file": size,
        "total_bytes": total_bytes,
        "push_mib_per_s": round(total_bytes / 1048576 / (push_ms / 1000), 3),
        "pull_mib_per_s": round(total_bytes / 1048576 / (pull_ms / 1000), 3),
    }
    return steps, notes


def _shape_interrupted_transfer(op: Operation, endpoint: EndpointContext, executor: Executor, attempt: int, tmp_root: Path) -> tuple[list[Step], dict[str, Any]]:
    params = op.params
    direction = str(params.get("direction", "pull"))
    files = int(params.get("files", 32))
    size = int(params.get("bytes_per_file", 64 * 1024))
    interrupt_after_ms = int(params.get("interrupt_after_ms", 1500))
    kill_mode = str(params.get("kill_mode", "wrapper"))
    remote_dir = f"{endpoint.scratch}/interrupt-{op.id}-{attempt:03d}"
    seed = f"{endpoint.run_id}-{op.id}-{attempt}"
    steps: list[Step] = []
    notes: dict[str, Any] = {"direction": direction, "files": files, "bytes_per_file": size, "kill_mode": kill_mode}
    with tempfile.TemporaryDirectory(prefix="vaws-maturation-", dir=str(tmp_root)) as tmp:
        if direction == "pull":
            steps.append(
                executor.call(
                    "seed-remote",
                    "remote.bash",
                    {"command": _remote_make_files_command(remote_dir, files, size, seed)},
                    {"outcome": "success", "status": "ok", "stdout_contains": f"made {files}"},
                    timeout_ms=op.timeout_ms,
                )
            )
            local_dir = Path(tmp) / "pulled"
            interrupted = executor.call(
                "interrupted-pull",
                "remote.artifact_pull",
                {"remote_path": remote_dir, "local_dir": str(local_dir)},
                {},
                timeout_ms=op.timeout_ms,
                via="cli",
                kill_after_ms=interrupt_after_ms,
                kill_mode=kill_mode,
                expect_killed=True,
            )
            steps.append(interrupted)
            notes["interrupted"] = bool(interrupted.extra.get("killed"))
            if kill_mode == "transport" and interrupted.result is not None:
                # A dropped connection must surface as a clean, parsable failure.
                clean = interrupted.result.get("outcome") in {"failed", "timeout"}
                steps.append(_verify_step("verify-clean-failure-report", clean, f"wrapper reported outcome={interrupted.result.get('outcome')!r} after transport drop", status="partial_state"))
            leftovers = sorted(str(p.relative_to(local_dir)) for p in local_dir.rglob("*.tmp")) if local_dir.exists() else []
            partial = len(list(local_dir.glob("blob-*.bin"))) if local_dir.exists() else 0
            notes["partial_files_after_interrupt"] = partial
            notes["leftover_temp_files"] = leftovers
            steps.append(_verify_step("verify-no-temp-leftovers", not leftovers, f"temporary files left after interruption: {leftovers[:5]}", status="leftover_temp_files"))
            rerun = executor.call(
                "rerun-pull",
                "remote.artifact_pull",
                {"remote_path": remote_dir, "local_dir": str(local_dir)},
                {"outcome": "success", "status": "ok"},
                timeout_ms=op.timeout_ms,
            )
            steps.append(rerun)
            artifacts = (rerun.result or {}).get("artifacts") or []
            skipped = len((artifacts[0] if artifacts and isinstance(artifacts[0], Mapping) else {}).get("skipped") or [])
            notes["rerun_skipped_hash_match"] = skipped
            expected = _manifest_hashes(rerun.result) if rerun.result and rerun.result.get("manifest") else None
            if expected is None and artifacts and isinstance(artifacts[0], Mapping):
                expected = {str(i.get("relpath")): str(i.get("sha256")) for i in (artifacts[0].get("manifest") or {}).get("files", []) if isinstance(i, Mapping)}
            pulled = {path.name: _sha256(path) for path in local_dir.glob("blob-*.bin")} if local_dir.exists() else {}
            steps.append(_verify_step("verify-final-hashes", bool(expected) and pulled == expected and len(pulled) == files, f"final local files do not match remote manifest ({len(pulled)} local, {len(expected or {})} remote, {files} expected)", status="hash_mismatch"))
        elif direction == "push":
            src = Path(tmp) / "src"
            expected = _make_local_files(src, files, size, seed=seed)
            interrupted = executor.call(
                "interrupted-push",
                "remote.artifact_push",
                {"local_path": str(src), "remote_path": remote_dir},
                {},
                timeout_ms=op.timeout_ms,
                via="cli",
                kill_after_ms=interrupt_after_ms,
                kill_mode=kill_mode,
                expect_killed=True,
            )
            steps.append(interrupted)
            notes["interrupted"] = bool(interrupted.extra.get("killed"))
            if kill_mode == "transport" and interrupted.result is not None:
                clean = interrupted.result.get("outcome") in {"failed", "timeout"}
                steps.append(_verify_step("verify-clean-failure-report", clean, f"wrapper reported outcome={interrupted.result.get('outcome')!r} after transport drop", status="partial_state"))
            leftover_step = executor.call(
                "inspect-remote-leftovers",
                "remote.bash",
                {"command": f"if [ -d {shlex.quote(remote_dir)} ]; then find {shlex.quote(remote_dir)} -name '*.tmp-*' | wc -l; else echo 0; fi"},
                {"outcome": "success", "status": "ok"},
                timeout_ms=30000,
            )
            steps.append(leftover_step)
            leftover_count = _last_int(result_text(leftover_step.payload, leftover_step.result))
            notes["leftover_temp_files"] = leftover_count
            steps.append(_verify_step("verify-no-temp-leftovers", leftover_count == 0, f"{leftover_count} temporary files left on remote after interruption", status="leftover_temp_files"))
            rerun = executor.call(
                "rerun-push",
                "remote.artifact_push",
                {"local_path": str(src), "remote_path": remote_dir},
                {"outcome": "success", "status": "ok"},
                timeout_ms=op.timeout_ms,
            )
            steps.append(rerun)
            manifest = executor.call(
                "manifest",
                "remote.artifact_manifest",
                {"remote_path": remote_dir},
                {"outcome": "success", "status": "ok", "file_count": files},
                timeout_ms=op.timeout_ms,
            )
            steps.append(manifest)
            steps.append(_verify_step("verify-final-hashes", _manifest_hashes(manifest.result) == expected, "remote manifest after rerun does not match local source", status="hash_mismatch"))
        else:
            raise ValueError(f"unsupported interrupted_transfer direction {direction!r}")
    steps.append(
        executor.call(
            "cleanup",
            "remote.bash",
            {"command": f"rm -rf {shlex.quote(remote_dir)}"},
            {"outcome": "success", "status": "ok"},
            timeout_ms=30000,
        )
    )
    return steps, notes


def _last_int(text: str) -> int:
    for line in reversed(text.strip().splitlines()):
        token = line.strip()
        if token.isdigit():
            return int(token)
    return -1


def _shape_idempotent_rerun(op: Operation, endpoint: EndpointContext, executor: Executor, attempt: int, tmp_root: Path) -> tuple[list[Step], dict[str, Any]]:
    assert op.tool is not None
    ctx = endpoint.template_context(attempt)
    params = op.params
    steps: list[Step] = []
    setup = params.get("setup")
    if isinstance(setup, Mapping):
        steps.append(
            executor.call(
                "setup",
                str(setup.get("tool", "remote.bash")),
                render(dict(setup.get("args", {})), ctx),
                render(dict(setup.get("expect", {"outcome": "success"})), ctx),
                timeout_ms=op.timeout_ms,
            )
        )
    args = render(op.args, ctx)
    steps.append(executor.call("first", op.tool, args, render(op.expect, ctx), timeout_ms=op.timeout_ms, via=op.via))
    reruns = int(params.get("reruns", 1))
    second_expect = render(dict(params.get("second_expect", op.expect)), ctx)
    for index in range(reruns):
        steps.append(executor.call(f"rerun-{index + 1}", op.tool, args, second_expect, timeout_ms=op.timeout_ms, via=op.via))
    verify = params.get("verify")
    if isinstance(verify, Mapping):
        steps.append(
            executor.call(
                "verify",
                str(verify.get("tool", "remote.bash")),
                render(dict(verify.get("args", {})), ctx),
                render(dict(verify.get("expect", {"outcome": "success"})), ctx),
                timeout_ms=op.timeout_ms,
            )
        )
    return steps, {"reruns": reruns}


def _shape_job_registry(op: Operation, endpoint: EndpointContext, executor: Executor, attempt: int, tmp_root: Path) -> tuple[list[Step], dict[str, Any]]:
    params = op.params
    ctx = endpoint.template_context(attempt)
    command = render(str(params.get("command", "sleep 2; printf 'job-done-{trial}\\n'")), ctx)
    tail_contains = render(str(params.get("tail_contains", "job-done-{trial}")), ctx)
    poll_interval = float(params.get("poll_interval_ms", 500)) / 1000
    poll_timeout_ms = int(params.get("poll_timeout_ms", 30000))
    steps: list[Step] = []
    start_step = executor.call(
        "start",
        "remote.bash",
        {"command": command, "run_in_background": True},
        {"outcome": "success", "status": "running"},
        timeout_ms=op.timeout_ms,
    )
    steps.append(start_step)
    job = ((start_step.result or {}).get("job") or {}) if start_step.result else {}
    job_id = job.get("job_id")
    notes: dict[str, Any] = {"job_id": job_id}
    if not job_id:
        return steps, notes
    deadline = time.monotonic() + poll_timeout_ms / 1000
    polls = 0
    status_step: Step | None = None
    while time.monotonic() < deadline:
        polls += 1
        status_step = executor.call(
            f"status-{polls}",
            "remote.job_status",
            {"job_id": job_id},
            {"outcome": "success"},
            timeout_ms=20000,
        )
        current = (status_step.result or {}).get("status")
        if current not in {"running", None} or not status_step.passed:
            break
        time.sleep(poll_interval)
    notes["polls"] = polls
    if status_step is not None:
        final_expect = {"outcome": "success", "status": str(params.get("final_status", "succeeded"))}
        status_step.expectation_error = evaluate(status_step.result, final_expect, "")
        status_step.passed = status_step.expectation_error is None and status_step.exception is None
        status_step.name = "status-final"
        steps.append(status_step)
    steps.append(
        executor.call(
            "tail",
            "remote.job_tail",
            {"job_id": job_id, "lines": 20},
            {"outcome": "success", "status": "ok", "stdout_contains": tail_contains},
            timeout_ms=20000,
        )
    )
    steps.append(
        executor.call(
            "status-again",
            "remote.job_status",
            {"job_id": job_id},
            {"outcome": "success", "status": str(params.get("final_status", "succeeded"))},
            timeout_ms=20000,
        )
    )
    if params.get("stop"):
        long_step = executor.call(
            "start-long",
            "remote.bash",
            {"command": str(params.get("long_command", "sleep 120")), "run_in_background": True},
            {"outcome": "success", "status": "running"},
            timeout_ms=op.timeout_ms,
        )
        steps.append(long_step)
        long_job = (((long_step.result or {}).get("job") or {}) if long_step.result else {}).get("job_id")
        if long_job:
            time.sleep(1.0)
            steps.append(
                executor.call(
                    "stop",
                    "remote.job_stop",
                    {"job_id": long_job},
                    {"outcome": "cancelled", "status": "cancelled"},
                    timeout_ms=20000,
                )
            )
            steps.append(
                executor.call(
                    "status-after-stop",
                    "remote.job_status",
                    {"job_id": long_job},
                    {"outcome": "success", "status": "cancelled"},
                    timeout_ms=20000,
                )
            )
    return steps, notes


SHAPE_RUNNERS: dict[str, Callable[..., tuple[list[Step], dict[str, Any]]]] = {
    "baseline": _shape_baseline,
    "long_stream": _shape_baseline,
    "concurrent": _shape_concurrent,
    "multi_session": _shape_concurrent,
    "stream_under_load": _shape_stream_under_load,
    "large_transfer": _shape_large_transfer,
    "interrupted_transfer": _shape_interrupted_transfer,
    "idempotent_rerun": _shape_idempotent_rerun,
    "job_registry": _shape_job_registry,
}

__all__ = [
    "EndpointContext",
    "Executor",
    "SHAPE_RUNNERS",
    "Step",
    "evaluate",
    "result_text",
    "run_operation",
    "utc_now_iso",
]
