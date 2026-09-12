"""Run selected local pytest suites with progress, retained results and owned children."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from remote_dev.core.local_process import OwnedProcess

SCHEMA = 2
FINAL = {"passed", "failed", "timed_out", "interrupted", "error", "not_run"}


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.is_file():
        return "missing"
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_receipt(path: Path, receipt: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def junit_counts(path: Path) -> dict:
    root = ET.parse(path).getroot()
    suites = list(root.iter("testsuite"))
    if not suites:
        raise ValueError("JUnit has no test suite")
    counts = {key: sum(int(suite.attrib.get(key, 0)) for suite in suites)
              for key in ("tests", "failures", "errors", "skipped")}
    if counts["tests"] <= 0 or any(value < 0 for value in counts.values()):
        raise ValueError("JUnit has no completed tests or invalid counts")
    return counts


@contextmanager
def owned_process(command: list[str], **kwargs):
    # The CLI has already entered its prepared dependency environment. Reuse
    # the package's Windows job and POSIX group ownership, including cleanup
    # after the direct child exits, instead of maintaining another signal path.
    with OwnedProcess(command, **kwargs) as owner:
        yield owner.process


def select_cases(root: Path, selections: list[str], split: str) -> list[str]:
    root = Path(root).resolve()
    selections = selections or [".agents/tests", *[
        str(path.relative_to(root)) for path in sorted((root / ".agents/skills").glob("*/tests"))
    ]]
    found = set()
    for selected in selections:
        path = (root / selected).resolve()
        if not path.is_relative_to(root) or not path.exists():
            raise ValueError(f"test selection must exist inside the repository: {selected}")
        if path.is_file():
            found.add(path.relative_to(root).as_posix())
        elif split == "file" or path == root / ".agents/tests":
            found.update(item.relative_to(root).as_posix() for item in path.rglob("test_*.py"))
        else:
            found.add(path.relative_to(root).as_posix())
    if not found:
        raise ValueError("no test files selected")
    return sorted(found)


def run(root: Path, cases: list[str], *, jobs: int = 1, timeout: float = 600,
        heartbeat: float = 10, pytest_args: list[str] | None = None,
        progress=sys.stderr) -> tuple[dict, int]:
    pytest_args = pytest_args or []
    started = time.monotonic()
    output = root / ".vaws-local/test-runs" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    receipt_path = output / "summary.json"
    receipt = {"schema_version": SCHEMA, "summary": str(receipt_path), "status": "running",
               "root": str(root.resolve()), "pytest_args": pytest_args,
               "settings": {"jobs": jobs, "timeout_seconds": timeout, "heartbeat_seconds": heartbeat},
               "cases": [{"case": case, "status": "not_run",
                          "log": str(output / f"{index:04d}.log"),
                          "junit": str(output / f"{index:04d}.xml"),
                          "pytest_exit_code": None, "elapsed_seconds": 0.0}
                         for index, case in enumerate(cases)], "elapsed_seconds": 0.0}
    pending = list(receipt["cases"])
    active = []
    interrupted = []
    handlers = {}

    def stop(signum, frame):
        interrupted.append(signum)

    def save():
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_receipt(receipt_path, receipt)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, stop)
        save()
        while pending or active:
            while pending and len(active) < jobs and not interrupted:
                row = pending.pop(0)
                row["status"] = "running"
                command = [sys.executable, "-X", "utf8", "-m", "pytest", row["case"], "-q", *pytest_args,
                           "--junitxml=" + row["junit"]]
                log = Path(row["log"]).open("wb")
                context = owned_process(command, cwd=root, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT)
                try:
                    process = context.__enter__()
                except Exception as exc:
                    log.close()
                    row.update(status="error", error=str(exc), log_sha256=digest_file(Path(row["log"])))
                else:
                    active.append((row, process, context, log, time.monotonic(), time.monotonic()))
                    print(f"[start] {row['case']} pid={process.pid} log={row['log']}", file=progress, flush=True)
                save()
            for entry in list(active):
                row, process, context, log, began, notified = entry
                now = time.monotonic()
                elapsed = now - began
                code = process.poll()
                status = "interrupted" if interrupted else "timed_out" if elapsed >= timeout and code is None else None
                if code is not None or status:
                    context.__exit__(None, None, None)
                    log.close()
                    row.update(status=status or ("passed" if code == 0 else "failed"),
                               pytest_exit_code=code if code is not None else process.returncode,
                               elapsed_seconds=round(time.monotonic() - began, 3))
                    try:
                        row["counts"] = junit_counts(Path(row["junit"]))
                        if row["status"] == "passed" and (row["counts"]["failures"] or row["counts"]["errors"]):
                            raise ValueError("JUnit contradicts successful pytest exit")
                    except (OSError, ValueError, ET.ParseError) as exc:
                        if row["status"] == "passed":
                            row.update(status="error", error=str(exc))
                    for key in ("log", "junit"):
                        row[key + "_sha256"] = digest_file(Path(row[key]))
                    active.remove(entry)
                    print(f"[{row['status']}] {row['case']} {row['elapsed_seconds']:.1f}s exit={row['pytest_exit_code']} log={row['log']}", file=progress, flush=True)
                    save()
                elif now - notified >= heartbeat:
                    row["elapsed_seconds"] = round(elapsed, 3)
                    print(f"[running] {row['case']} {elapsed:.1f}s log={row['log']}", file=progress, flush=True)
                    active[active.index(entry)] = (*entry[:5], now)
                    save()
            if interrupted and not active:
                break
            if pending or active:
                time.sleep(min(0.1, heartbeat))
    finally:
        for row, process, context, log, began, notified in active:
            context.__exit__(None, None, None)
            log.close()
            row.update(status="interrupted", pytest_exit_code=process.returncode,
                       elapsed_seconds=round(time.monotonic() - began, 3))
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        receipt["status"] = "interrupted" if interrupted else (
            "passed" if all(row["status"] == "passed" for row in receipt["cases"]) else "failed")
        save()
    return receipt, 128 + interrupted[0] if interrupted else (0 if receipt["status"] == "passed" else 1)


def main(root: Path, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="repository test files or directories")
    parser.add_argument("--split", choices=("suite", "file"), default="suite")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=600, help="seconds per subprocess")
    parser.add_argument("--heartbeat", type=float, default=10, help="seconds between progress lines")
    parser.add_argument("--rerun-failed", type=Path, help="select unfinished or unsuccessful cases from a previous summary; previous passes are not revalidated")
    parser.add_argument("--pytest-arg", action="append", default=[], help="repeatable pytest argument; use = for options")
    args = parser.parse_args(argv)
    if (not 1 <= args.jobs <= 32 or not math.isfinite(args.timeout) or args.timeout <= 0
            or not math.isfinite(args.heartbeat) or args.heartbeat <= 0):
        parser.error("jobs must be 1..32; timeout and heartbeat must be positive and finite")
    try:
        extra = args.pytest_arg
        paths = args.paths
        if args.rerun_failed:
            if paths:
                parser.error("--rerun-failed selects cases from the summary; omit paths")
            previous = json.loads(args.rerun_failed.read_text(encoding="utf-8"))
            if not isinstance(previous, dict) or previous.get("schema_version") != SCHEMA:
                raise ValueError("unsupported previous summary schema")
            rows = previous.get("cases")
            original_args = previous.get("pytest_args")
            if (not isinstance(rows, list) or not all(isinstance(row, dict)
                    and isinstance(row.get("case"), str) and row.get("status") in FINAL | {"running"} for row in rows)
                    or not isinstance(original_args, list) or not all(isinstance(arg, str) for arg in original_args)):
                raise ValueError("malformed previous summary")
            paths = [row["case"] for row in rows if row["status"] != "passed"]
            if not paths:
                print(json.dumps({"schema_version": SCHEMA, "status": "nothing_to_rerun", "cases": [], "previous_summary": str(args.rerun_failed)}))
                return 0
            extra = extra or original_args
        cases = select_cases(root, paths, args.split)
        result, code = run(root, cases, jobs=args.jobs, timeout=args.timeout,
                           heartbeat=args.heartbeat, pytest_args=extra)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"schema_version": SCHEMA, "status": "error", "error": str(exc)}))
        return 2
    print(json.dumps(result))
    return code
