"""Local pytest subprocesses with progress, receipts and conservative pass reuse."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse
import uuid
import xml.etree.ElementTree as ET

SCHEMA = 1
FINAL = {"passed", "failed", "timed_out", "interrupted", "error", "not_run"}


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.is_file():
        return "missing"
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_json(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def source_digest(root: Path, on_progress=None) -> str:
    """Hash actual tracked/untracked source bytes, recursively including gitlinks."""
    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root,
    ).decode("utf-8", errors="surrogateescape").split("\0")
    # Uninitialized submodules still have meaningful pinned source identities.
    index = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=root)
    values = [("gitlink", row.decode("utf-8", errors="surrogateescape"))
              for row in index.split(b"\0") if row.startswith(b"160000 ")]
    for number, name in enumerate(sorted(set(names) - {""})):
        if on_progress and number % 128 == 0:
            on_progress(f"source files checked: {number}")
        path = root / name
        value = source_digest(path, on_progress) if path.is_dir() and (path / ".git").exists() else digest_file(path)
        values.append((name, value, os.readlink(path) if path.is_symlink() else None))
    return digest_json(values)


def installed_file_signature(path: Path):
    try:
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino]
    except FileNotFoundError:
        return "missing"


def fingerprint(root: Path, cases: list[str], pytest_args: list[str], on_progress=None) -> dict:
    """Hash sources; identify installed dependencies by RECORD and file metadata."""
    packages = []
    for dist in importlib.metadata.distributions():
        files = []
        for number, path in enumerate(sorted(dist.files or [], key=str)):
            if str(path).endswith(".pyc"):
                continue
            if on_progress and number % 128 == 0:
                on_progress(f"checking dependency {dist.metadata['Name']}: {number} files")
            files.append((str(path), installed_file_signature(Path(dist.locate_file(path)))))
        metadata = [dist.read_text(name) for name in ("METADATA", "RECORD", "direct_url.json")]
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        editable = None
        if direct.get("dir_info", {}).get("editable"):
            parsed = urlparse(direct["url"])
            path = unquote(parsed.path)
            if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
                path = path[1:]
            editable = source_digest(Path(path), on_progress)
        packages.append((dist.metadata["Name"], dist.version, digest_json([files, metadata]), editable))
    parts = {
        "root": str(root), "sources": source_digest(root, on_progress),
        "dependencies": digest_json(sorted(packages)),
        "dependency_method": "installed RECORD, metadata and file stat; editable source bytes",
        "python": [sys.executable, sys.version], "platform": platform.platform(),
        "environment": digest_json(dict(os.environ)),
        "cases": cases, "pytest_args": pytest_args,
    }
    return {"digest": digest_json(parts), "parts": parts}


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
    if os.name == "nt":
        from vaws_windows import owned_process as windows_process
        with windows_process(command, **kwargs) as process:
            yield process
        return
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        yield process
    finally:
        # Always drain this group, including grandchildren left by a failed test.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def select_cases(root: Path, selections: list[str], split: str) -> list[str]:
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


def reusable(row: dict) -> bool:
    try:
        return (row["status"] == "passed" and row["pytest_exit_code"] == 0
                and all(digest_file(Path(row[key])) == row[key + "_sha256"]
                        and row[key + "_sha256"] != "missing" for key in ("log", "junit"))
                and junit_counts(Path(row["junit"])) == row["counts"])
    except (KeyError, TypeError, OSError, ValueError, ET.ParseError):
        return False


def run(root: Path, cases: list[str], *, jobs: int = 1, timeout: float = 600,
        heartbeat: float = 10, pytest_args: list[str] | None = None,
        previous: dict | None = None, progress=sys.stderr) -> tuple[dict, int]:
    pytest_args = pytest_args or []
    started = time.monotonic()
    output = root / ".vaws-local/test-runs" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    receipt_path = output / "summary.json"
    print(f"[prepare] fingerprinting sources and dependencies; summary={receipt_path}", file=progress, flush=True)
    preparation = {"schema_version": SCHEMA, "summary": str(receipt_path),
                   "status": "preparing", "cases": [], "elapsed_seconds": 0.0}
    write_receipt(receipt_path, preparation)
    last_progress = started

    def preparing(message):
        nonlocal last_progress
        now = time.monotonic()
        if now - last_progress >= heartbeat:
            last_progress = now
            preparation.update(elapsed_seconds=round(now - started, 3), preparation=message)
            print(f"[prepare] {message}; {now - started:.1f}s", file=progress, flush=True)
            write_receipt(receipt_path, preparation)

    try:
        identity = fingerprint(root, cases, pytest_args, on_progress=preparing)
    except BaseException as exc:
        preparation.update(status="error", error=str(exc), elapsed_seconds=round(time.monotonic() - started, 3))
        write_receipt(receipt_path, preparation)
        raise
    same = bool(previous and previous.get("fingerprint") == identity)
    old = {row["case"]: row for row in previous.get("cases", [])} if same else {}
    receipt = {"schema_version": SCHEMA, "summary": str(receipt_path), "status": "running",
               "preparation_seconds": round(time.monotonic() - started, 3),
               "fingerprint": identity, "reuse_allowed": same,
               "reuse_reason": "inputs match" if same else "new run or inputs changed",
               "settings": {"jobs": jobs, "timeout_seconds": timeout, "heartbeat_seconds": heartbeat},
               "cases": [], "elapsed_seconds": 0.0}
    for index, case in enumerate(cases):
        prior = old.get(case)
        if prior and reusable(prior):
            receipt["cases"].append(dict(prior, reused=True))
            print(f"[reuse] {case}", file=progress, flush=True)
        else:
            receipt["cases"].append({"case": case, "status": "not_run", "reused": False,
                "log": str(output / f"{index:04d}.log"), "junit": str(output / f"{index:04d}.xml"),
                "pytest_exit_code": None, "elapsed_seconds": 0.0})
    pending = [row for row in receipt["cases"] if not row["reused"]]
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
                command = [sys.executable, "-m", "pytest", row["case"], "-q", *pytest_args,
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
    parser.add_argument("--rerun-failed", type=Path, help="previous summary; changed inputs rerun all original cases")
    parser.add_argument("--pytest-arg", action="append", default=[], help="repeatable pytest argument; use = for options")
    args = parser.parse_args(argv)
    if (not 1 <= args.jobs <= 32 or not math.isfinite(args.timeout) or args.timeout <= 0
            or not math.isfinite(args.heartbeat) or args.heartbeat <= 0):
        parser.error("jobs must be 1..32; timeout and heartbeat must be positive and finite")
    try:
        previous = None
        if args.rerun_failed:
            if args.paths:
                parser.error("--rerun-failed uses the original selection; omit paths")
            previous = json.loads(args.rerun_failed.read_text(encoding="utf-8"))
            if not isinstance(previous, dict) or previous.get("schema_version") != SCHEMA:
                raise ValueError("unsupported previous summary schema")
            parts = previous["fingerprint"]["parts"]
            if (not isinstance(parts["cases"], list) or not all(isinstance(value, str) for value in parts["cases"])
                    or not isinstance(parts["pytest_args"], list)
                    or not all(isinstance(value, str) for value in parts["pytest_args"])
                    or not isinstance(previous["cases"], list)
                    or not all(isinstance(row, dict) and isinstance(row.get("case"), str) for row in previous["cases"])):
                raise ValueError("malformed previous summary")
            cases = select_cases(root, parts["cases"], "suite")
            extra = args.pytest_arg or parts["pytest_args"]
        else:
            cases = select_cases(root, args.paths, args.split)
            extra = args.pytest_arg
        result, code = run(root, cases, jobs=args.jobs, timeout=args.timeout,
                           heartbeat=args.heartbeat, pytest_args=extra, previous=previous)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"schema_version": SCHEMA, "status": "error", "error": str(exc)}))
        return 2
    print(json.dumps(result))
    return code
