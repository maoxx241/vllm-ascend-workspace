#!/usr/bin/env python3
"""Print expected and local sizes for explicit ModelScope model directories."""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

DEFAULT_IGNORE_OFFICIAL = {".gitattributes"}


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    local_dir: Path


def parse_model_spec(value: str) -> ModelSpec:
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            "model spec must be MODEL_ID=LOCAL_DIR, for example "
            "org/name=/path/to/local/model"
        )
    model_id, local_dir = value.split("=", 1)
    if not model_id or "/" not in model_id:
        raise argparse.ArgumentTypeError("MODEL_ID must look like namespace/name")
    if not local_dir:
        raise argparse.ArgumentTypeError("LOCAL_DIR must not be empty")
    return ModelSpec(model_id=model_id, local_dir=Path(local_dir))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show official expected size and local size for ModelScope models."
    )
    parser.add_argument(
        "--model",
        action="append",
        type=parse_model_spec,
        required=True,
        metavar="MODEL_ID=LOCAL_DIR",
        help="ModelScope model id and local directory. Repeat for more models.",
    )
    parser.add_argument("--revision", default="master")
    parser.add_argument(
        "--ignore-official",
        action="append",
        default=list(DEFAULT_IGNORE_OFFICIAL),
        help="Official relative path to skip. Default: .gitattributes",
    )
    return parser.parse_args()


def bytes_to_gib(value: int) -> str:
    return f"{value / (1024 ** 3):.2f} GiB"


def local_size_for_files(root: Path, files: list[dict[str, Any]]) -> int:
    if not root.exists():
        return 0
    total = 0
    for file_info in files:
        path = root / file_info["Path"]
        if path.exists() and path.is_file():
            total += path.stat().st_size
    return total


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
    files = data.get("Data", {}).get("Files", [])
    return [
        file_info
        for file_info in files
        if file_info.get("Type") == "blob" and file_info.get("Path")
    ]


def main() -> int:
    args = parse_args()
    ignore_official = set(args.ignore_official)
    for spec in args.model:
        files = [
            file_info
            for file_info in fetch_official_files(spec.model_id, args.revision)
            if file_info["Path"] not in ignore_official
        ]
        expected = sum(int(file_info.get("Size", 0)) for file_info in files)
        actual = local_size_for_files(spec.local_dir, files)
        percent = (actual / expected * 100) if expected else 0
        print(
            f"{spec.model_id}\n"
            f"  expected: {bytes_to_gib(expected)} in {len(files)} files\n"
            f"  local   : {bytes_to_gib(actual)} ({percent:.2f}%)\n"
            f"  dir     : {spec.local_dir}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
