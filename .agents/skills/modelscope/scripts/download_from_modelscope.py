#!/usr/bin/env python3
"""Download one ModelScope model to an explicit local directory."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path


PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be >= 1")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download one ModelScope model snapshot. The model id and local "
            "directory must be provided explicitly."
        )
    )
    parser.add_argument(
        "--model-id",
        required=True,
        help="ModelScope model id, for example namespace/name.",
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        required=True,
        help="Directory where the model files will be stored.",
    )
    parser.add_argument(
        "--revision",
        default=os.environ.get("REVISION", "master"),
        help="Model revision or branch. Default: master",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ["MODELSCOPE_CACHE"])
        if os.environ.get("MODELSCOPE_CACHE")
        else None,
        help="Optional ModelScope cache directory.",
    )
    parser.add_argument(
        "--max-retries",
        type=positive_int,
        default=int(os.environ.get("MAX_RETRIES", "3")),
        help="Maximum download attempts. Default: 3",
    )
    parser.add_argument(
        "--max-workers",
        type=positive_int,
        default=(
            int(os.environ["MODELSCOPE_MAX_WORKERS"])
            if os.environ.get("MODELSCOPE_MAX_WORKERS")
            else None
        ),
        help="Maximum ModelScope file workers. Default: SDK default.",
    )
    parser.add_argument(
        "--download-parallels",
        type=positive_int,
        default=int(os.environ.get("MODELSCOPE_DOWNLOAD_PARALLELS", "1")),
        help="Parallel HTTP range requests per large file. Default: 1",
    )
    parser.add_argument(
        "--parallel-threshold-mb",
        type=positive_int,
        default=int(os.environ.get("MODELSCOPE_PARALLEL_DOWNLOAD_THRESHOLD_MB", "500")),
        help="Use parallel range download for files larger than this size in MB. Default: 500",
    )
    parser.add_argument(
        "--proxy",
        default=os.environ.get("MODELSCOPE_PROXY"),
        help="Optional HTTP/HTTPS proxy for ModelScope requests.",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Clear proxy environment variables for this run.",
    )
    parser.add_argument(
        "--log-in-local-dir",
        action="store_true",
        help="Append output to LOCAL_DIR/download.log.",
    )
    parser.add_argument(
        "--auto-install",
        action="store_true",
        help="Install/upgrade modelscope with pip if it is missing.",
    )
    return parser.parse_args()


def configure_environment(args: argparse.Namespace) -> None:
    if args.no_proxy:
        for name in PROXY_VARS:
            os.environ.pop(name, None)
    elif args.proxy:
        for name in PROXY_VARS:
            os.environ[name] = args.proxy

    os.environ["MODELSCOPE_DOWNLOAD_PARALLELS"] = str(args.download_parallels)
    os.environ["MODELSCOPE_PARALLEL_DOWNLOAD_THRESHOLD_MB"] = str(
        args.parallel_threshold_mb
    )
    if args.cache_dir is not None:
        os.environ["MODELSCOPE_CACHE"] = str(args.cache_dir)


def ensure_modelscope(auto_install: bool) -> None:
    if importlib.util.find_spec("modelscope") is not None:
        return

    if not auto_install:
        raise RuntimeError(
            "modelscope is not installed. Install it first or pass --auto-install."
        )

    print("modelscope package not found; installing/upgrading with pip...", flush=True)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "modelscope"])


def redirect_logs(local_dir: Path) -> None:
    local_dir.mkdir(parents=True, exist_ok=True)
    log_path = local_dir / "download.log"
    log = log_path.open("a", encoding="utf-8", buffering=1)
    os.dup2(log.fileno(), sys.stdout.fileno())
    os.dup2(log.fileno(), sys.stderr.fileno())
    print(f"\n===== download restart {time.strftime('%Y-%m-%dT%H:%M:%S%z')} =====")


def download_with_retry(args: argparse.Namespace) -> Path:
    from modelscope import snapshot_download

    args.local_dir.mkdir(parents=True, exist_ok=True)
    if args.cache_dir is not None:
        args.cache_dir.mkdir(parents=True, exist_ok=True)

    if os.environ.get("MODELSCOPE_TOKEN") and not os.environ.get("MODELSCOPE_API_TOKEN"):
        os.environ["MODELSCOPE_API_TOKEN"] = os.environ["MODELSCOPE_TOKEN"]

    print(f"ModelScope model      : {args.model_id}")
    print(f"Revision              : {args.revision}")
    print(f"Download target       : {args.local_dir}")
    print(f"ModelScope cache      : {args.cache_dir if args.cache_dir is not None else 'SDK default'}")
    print(f"Proxy                 : {args.proxy if args.proxy and not args.no_proxy else 'disabled'}")
    print(f"File parallels        : {args.download_parallels}")
    print(f"Parallel threshold MB : {args.parallel_threshold_mb}")

    last_error: BaseException | None = None
    for attempt in range(1, args.max_retries + 1):
        print(f"Starting download attempt {attempt}/{args.max_retries}...", flush=True)
        try:
            kwargs = {
                "model_id": args.model_id,
                "revision": args.revision,
                "local_dir": str(args.local_dir),
                "max_workers": args.max_workers,
            }
            if args.cache_dir is not None:
                kwargs["cache_dir"] = str(args.cache_dir)
            model_dir = snapshot_download(**kwargs)
            print(f"Download completed: {model_dir}")
            return Path(model_dir)
        except Exception as exc:
            last_error = exc
            print(f"Attempt {attempt} failed: {exc}", file=sys.stderr, flush=True)
            if attempt < args.max_retries:
                time.sleep(10)

    raise RuntimeError(f"download failed after {args.max_retries} attempts") from last_error


def main() -> int:
    args = parse_args()
    configure_environment(args)
    ensure_modelscope(auto_install=args.auto_install)
    if args.log_in_local_dir:
        redirect_logs(args.local_dir)
    download_with_retry(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
