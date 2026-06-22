#!/usr/bin/env python3
"""Compact ModelScope task manager: status, resume, and verify."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


SKILL_DIR = Path(__file__).resolve().parents[1]
DOWNLOAD_SCRIPT = SKILL_DIR / "scripts" / "download_from_modelscope.py"
VERIFY_SCRIPT = SKILL_DIR / "scripts" / "verify_modelscope_sha256.py"
DEFAULT_IGNORE_OFFICIAL = {".gitattributes"}
DEFAULT_IGNORE_EXTRA = {
    ".gitattributes",
    "download.log",
    "download.pid",
    "download.launch.log",
    "verify.log",
    "modelscope_sha256.tsv",
    "modelscope_sha256.report.json",
    "SHA256SUMS",
}
PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    local_dir: Path


def parse_model_spec(value: str) -> ModelSpec:
    if "=" not in value:
        raise argparse.ArgumentTypeError("model must be MODEL_ID=LOCAL_DIR")
    model_id, local_dir = value.split("=", 1)
    if not model_id or "/" not in model_id:
        raise argparse.ArgumentTypeError("MODEL_ID must look like namespace/name")
    if not local_dir:
        raise argparse.ArgumentTypeError("LOCAL_DIR must not be empty")
    return ModelSpec(model_id=model_id, local_dir=Path(local_dir))


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be >= 1")
    return parsed


def bytes_to_gib(value: int) -> str:
    return f"{value / (1024 ** 3):.2f} GiB"


def request_json(url: str, *, retries: int = 5, timeout: int = 60) -> dict[str, Any]:
    session = requests.Session()
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if not data.get("Success", False):
                raise RuntimeError(f"ModelScope API returned failure: {data}")
            return data
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(30, attempt * 5))
    raise RuntimeError(f"failed to fetch {url}") from last_error


def fetch_official_files(model_id: str, revision: str) -> list[dict[str, Any]]:
    owner, name = model_id.split("/", 1)
    url = (
        f"https://modelscope.cn/api/v1/models/{quote(owner, safe='')}/"
        f"{quote(name, safe='')}/repo/files"
        f"?Revision={quote(revision, safe='')}&Recursive=true"
    )
    data = request_json(url)
    return [
        file_info
        for file_info in data.get("Data", {}).get("Files", [])
        if file_info.get("Type") == "blob"
        and file_info.get("Path")
        and file_info["Path"] not in DEFAULT_IGNORE_OFFICIAL
    ]


def local_size_for_files(root: Path, files: list[dict[str, Any]]) -> int:
    total = 0
    if not root.exists():
        return total
    for file_info in files:
        path = root / file_info["Path"]
        if path.is_file():
            total += path.stat().st_size
    return total


def pid_is_active(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid(local_dir: Path) -> int | None:
    try:
        text = (local_dir / "download.pid").read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def report_state(local_dir: Path) -> str:
    report_path = local_dir / "modelscope_sha256.report.json"
    if not report_path.exists():
        return "none"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    if data.get("all_ok") is True:
        return "ok"
    non_meta_failures = [
        check
        for check in data.get("checks", [])
        if check.get("status") != "ok"
        and check.get("path") not in DEFAULT_IGNORE_OFFICIAL
    ]
    return "failed" if non_meta_failures else "stale"


def inspect_model(spec: ModelSpec, revision: str) -> dict[str, Any]:
    files = fetch_official_files(spec.model_id, revision)
    expected = sum(int(file_info.get("Size", 0)) for file_info in files)
    actual = local_size_for_files(spec.local_dir, files)
    pid = read_pid(spec.local_dir)
    active = pid_is_active(pid)
    percent = (actual / expected * 100) if expected else 0.0
    complete = bool(expected and actual >= expected)
    verification = report_state(spec.local_dir)
    if active:
        state = "active"
    elif complete and verification == "ok":
        state = "verified"
    elif complete:
        state = "needs-verify"
    else:
        state = "needs-download"
    return {
        "model_id": spec.model_id,
        "local_dir": str(spec.local_dir),
        "expected": expected,
        "actual": actual,
        "percent": percent,
        "pid": pid,
        "active": active,
        "complete": complete,
        "verification": verification,
        "state": state,
    }


def print_status(result: dict[str, Any]) -> None:
    pid = result["pid"] if result["pid"] is not None else "-"
    print(
        f"{result['model_id']}\t{result['state']}\t"
        f"{result['percent']:.2f}%\t"
        f"{bytes_to_gib(result['actual'])}/{bytes_to_gib(result['expected'])}\t"
        f"pid={pid}\tverify={result['verification']}\t"
        f"dir={result['local_dir']}"
    )


def build_worker_env(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("TORCH_DEVICE_BACKEND_AUTOLOAD", "0")
    if args.no_proxy:
        for name in PROXY_VARS:
            env.pop(name, None)
    elif args.proxy:
        for name in PROXY_VARS:
            env[name] = args.proxy
    return env


def launch_worker(
    spec: ModelSpec,
    args: argparse.Namespace,
    *,
    verify_only: bool,
) -> int:
    spec.local_dir.mkdir(parents=True, exist_ok=True)
    launch_log = (spec.local_dir / "download.launch.log").open(
        "ab", buffering=0
    )
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "worker",
        "--model",
        f"{spec.model_id}={spec.local_dir}",
        "--revision",
        args.revision,
        "--max-retries",
        str(args.max_retries),
        "--download-parallels",
        str(args.download_parallels),
        "--parallel-threshold-mb",
        str(args.parallel_threshold_mb),
    ]
    if args.max_workers is not None:
        cmd.extend(["--max-workers", str(args.max_workers)])
    if args.auto_install:
        cmd.append("--auto-install")
    if verify_only:
        cmd.append("--verify-only")
    if args.no_proxy:
        cmd.append("--no-proxy")
    elif args.proxy:
        cmd.extend(["--proxy", args.proxy])

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=launch_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=build_worker_env(args),
    )
    (spec.local_dir / "download.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
    return proc.pid


def run_verify(spec: ModelSpec, revision: str) -> int:
    verify_log = spec.local_dir / "verify.log"
    verify_log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(VERIFY_SCRIPT),
        "--model",
        f"{spec.model_id}={spec.local_dir}",
        "--revision",
        revision,
        "--output-dir",
        str(spec.local_dir),
        "--output-prefix",
        "modelscope_sha256",
        "--write-model-sha256sums",
    ]
    for rel_path in sorted(DEFAULT_IGNORE_EXTRA):
        cmd.extend(["--ignore-extra", rel_path])
    with verify_log.open("ab", buffering=0) as log:
        return subprocess.call(cmd, stdout=log, stderr=subprocess.STDOUT)


def run_worker(args: argparse.Namespace) -> int:
    spec = args.model[0]
    env = build_worker_env(args)
    if not args.verify_only:
        download_cmd = [
            sys.executable,
            str(DOWNLOAD_SCRIPT),
            "--model-id",
            spec.model_id,
            "--local-dir",
            str(spec.local_dir),
            "--revision",
            args.revision,
            "--max-retries",
            str(args.max_retries),
            "--download-parallels",
            str(args.download_parallels),
            "--parallel-threshold-mb",
            str(args.parallel_threshold_mb),
            "--log-in-local-dir",
        ]
        if args.max_workers is not None:
            download_cmd.extend(["--max-workers", str(args.max_workers)])
        if args.auto_install:
            download_cmd.append("--auto-install")
        if args.no_proxy:
            download_cmd.append("--no-proxy")
        elif args.proxy:
            download_cmd.extend(["--proxy", args.proxy])
        download_rc = subprocess.call(download_cmd, env=env)
        if download_rc != 0:
            return download_rc
    return run_verify(spec, args.revision)


def discover_models(root: Path) -> list[ModelSpec]:
    specs: list[ModelSpec] = []
    for pid_path in sorted(root.rglob("download.pid")):
        model_dir = pid_path.parent
        try:
            rel_parts = model_dir.relative_to(root).parts
        except ValueError:
            continue
        if len(rel_parts) < 2:
            continue
        model_id = f"{rel_parts[-2]}/{rel_parts[-1]}"
        specs.append(ModelSpec(model_id=model_id, local_dir=model_dir))
    return specs


def resolve_models(args: argparse.Namespace) -> list[ModelSpec]:
    specs = list(args.model or [])
    if args.root is not None:
        specs.extend(discover_models(args.root))
    unique: dict[tuple[str, str], ModelSpec] = {}
    for spec in specs:
        unique[(spec.model_id, str(spec.local_dir))] = spec
    if not unique:
        raise SystemExit("provide --model MODEL_ID=LOCAL_DIR or --root ROOT")
    return list(unique.values())


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        action="append",
        type=parse_model_spec,
        metavar="MODEL_ID=LOCAL_DIR",
        help="Repeat for multiple models.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Discover tasks under ROOT by */download.pid and infer namespace/name.",
    )
    parser.add_argument("--revision", default="master")
    parser.add_argument("--proxy")
    parser.add_argument("--no-proxy", action="store_true")
    parser.add_argument("--max-retries", type=positive_int, default=3)
    parser.add_argument("--max-workers", type=positive_int)
    parser.add_argument("--download-parallels", type=positive_int, default=1)
    parser.add_argument("--parallel-threshold-mb", type=positive_int, default=500)
    parser.add_argument("--auto-install", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auto-manage ModelScope downloads with compact output."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("ensure", "status", "verify"):
        subparser = subparsers.add_parser(name)
        add_common_args(subparser)

    worker = subparsers.add_parser("worker", help="internal worker")
    add_common_args(worker)
    worker.add_argument("--verify-only", action="store_true")
    return parser.parse_args()


def command_status(args: argparse.Namespace) -> int:
    for spec in resolve_models(args):
        print_status(inspect_model(spec, args.revision))
    return 0


def command_ensure(args: argparse.Namespace) -> int:
    for spec in resolve_models(args):
        result = inspect_model(spec, args.revision)
        if result["state"] == "needs-download":
            pid = launch_worker(spec, args, verify_only=False)
            result["state"] = "download-started"
            result["pid"] = pid
        elif result["state"] == "needs-verify":
            if result["verification"] == "failed":
                result["state"] = "verify-failed"
            else:
                pid = launch_worker(spec, args, verify_only=True)
                result["state"] = "verify-started"
                result["pid"] = pid
        print_status(result)
    return 0


def command_verify(args: argparse.Namespace) -> int:
    rc = 0
    for spec in resolve_models(args):
        verify_rc = run_verify(spec, args.revision)
        result = inspect_model(spec, args.revision)
        print_status(result)
        rc = rc or verify_rc
    return rc


def main() -> int:
    args = parse_args()
    if args.command == "worker":
        return run_worker(args)
    if args.command == "status":
        return command_status(args)
    if args.command == "ensure":
        return command_ensure(args)
    if args.command == "verify":
        return command_verify(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
