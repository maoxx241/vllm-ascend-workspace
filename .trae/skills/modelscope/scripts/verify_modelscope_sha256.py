#!/usr/bin/env python3
"""Verify explicit local ModelScope model directories by official SHA256 metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

DEFAULT_IGNORE_OFFICIAL = (".gitattributes",)


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    local_dir: Path


@dataclass
class FileCheck:
    model_id: str
    local_dir: str
    path: str
    size_expected: int
    size_actual: int | None
    sha256_expected: str
    sha256_actual: str | None
    status: str


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


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be >= 1")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare local ModelScope downloads against official SHA256 metadata."
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
        "--chunk-size",
        type=positive_int,
        default=16 * 1024 * 1024,
        help="Bytes read per SHA256 update. Default: 16777216",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory where aggregate verification outputs are written.",
    )
    parser.add_argument(
        "--output-prefix",
        default="modelscope_sha256",
        help="Aggregate output filename prefix. Default: modelscope_sha256",
    )
    parser.add_argument(
        "--write-model-sha256sums",
        action="store_true",
        help="Write LOCAL_DIR/SHA256SUMS for each verified model.",
    )
    parser.add_argument(
        "--ignore-extra",
        action="append",
        default=[],
        help="Extra local relative path to ignore. Repeat as needed.",
    )
    parser.add_argument(
        "--ignore-official",
        action="append",
        default=list(DEFAULT_IGNORE_OFFICIAL),
        help=(
            "Official relative path to skip during verification. Repeat as "
            "needed. Default: .gitattributes"
        ),
    )
    return parser.parse_args()


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


def sha256_file(path: Path, chunk_size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(
    spec: ModelSpec,
    revision: str,
    chunk_size: int,
    ignore_extra: set[str],
    ignore_official: set[str],
) -> tuple[list[FileCheck], dict[str, Any]]:
    official_files = [
        file_info
        for file_info in fetch_official_files(spec.model_id, revision)
        if file_info["Path"] not in ignore_official
    ]
    checks: list[FileCheck] = []

    expected_paths = {file_info["Path"] for file_info in official_files}
    for file_info in official_files:
        rel_path = file_info["Path"]
        local_path = spec.local_dir / rel_path
        expected_size = int(file_info.get("Size", 0))
        expected_sha = str(file_info.get("Sha256", "")).lower()

        if not local_path.exists():
            checks.append(
                FileCheck(
                    model_id=spec.model_id,
                    local_dir=str(spec.local_dir),
                    path=rel_path,
                    size_expected=expected_size,
                    size_actual=None,
                    sha256_expected=expected_sha,
                    sha256_actual=None,
                    status="missing",
                )
            )
            continue

        actual_size = local_path.stat().st_size
        if actual_size != expected_size:
            checks.append(
                FileCheck(
                    model_id=spec.model_id,
                    local_dir=str(spec.local_dir),
                    path=rel_path,
                    size_expected=expected_size,
                    size_actual=actual_size,
                    sha256_expected=expected_sha,
                    sha256_actual=None,
                    status="size_mismatch",
                )
            )
            continue

        actual_sha = sha256_file(local_path, chunk_size)
        status = "ok" if actual_sha == expected_sha else "sha256_mismatch"
        checks.append(
            FileCheck(
                model_id=spec.model_id,
                local_dir=str(spec.local_dir),
                path=rel_path,
                size_expected=expected_size,
                size_actual=actual_size,
                sha256_expected=expected_sha,
                sha256_actual=actual_sha,
                status=status,
            )
        )

    extra_files = []
    if spec.local_dir.exists():
        for path in sorted(spec.local_dir.rglob("*")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(spec.local_dir).as_posix()
            if rel_path in ignore_extra:
                continue
            if rel_path not in expected_paths:
                extra_files.append(rel_path)

    summary = {
        "model_id": spec.model_id,
        "local_dir": str(spec.local_dir),
        "official_file_count": len(official_files),
        "ignored_official": sorted(ignore_official),
        "ok": sum(1 for check in checks if check.status == "ok"),
        "missing": sum(1 for check in checks if check.status == "missing"),
        "size_mismatch": sum(1 for check in checks if check.status == "size_mismatch"),
        "sha256_mismatch": sum(1 for check in checks if check.status == "sha256_mismatch"),
        "extra_file_count": len(extra_files),
        "extra_files": extra_files,
    }
    return checks, summary


def write_outputs(
    output_dir: Path,
    output_prefix: str,
    checks: list[FileCheck],
    summaries: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    tsv_path = output_dir / f"{output_prefix}.tsv"
    json_path = output_dir / f"{output_prefix}.report.json"
    sha256sums_path = output_dir / "SHA256SUMS"

    with tsv_path.open("w", encoding="utf-8") as handle:
        handle.write(
            "model_id\tlocal_dir\tpath\tsize_expected\tsize_actual\t"
            "sha256_expected\tsha256_actual\tstatus\n"
        )
        for check in checks:
            handle.write(
                f"{check.model_id}\t{check.local_dir}\t{check.path}\t"
                f"{check.size_expected}\t"
                f"{'' if check.size_actual is None else check.size_actual}\t"
                f"{check.sha256_expected}\t"
                f"{'' if check.sha256_actual is None else check.sha256_actual}\t"
                f"{check.status}\n"
            )

    with sha256sums_path.open("w", encoding="utf-8") as handle:
        for check in checks:
            if check.sha256_actual:
                handle.write(f"{check.sha256_actual}  {check.model_id}/{check.path}\n")

    report = {
        "generated_at_epoch": int(time.time()),
        "summaries": summaries,
        "all_ok": all(
            summary["missing"] == 0
            and summary["size_mismatch"] == 0
            and summary["sha256_mismatch"] == 0
            for summary in summaries
        ),
        "checks": [asdict(check) for check in checks],
    }
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    return tsv_path, json_path, sha256sums_path


def write_model_sha256sums(checks: list[FileCheck]) -> None:
    by_dir: dict[str, list[FileCheck]] = {}
    for check in checks:
        if check.sha256_actual:
            by_dir.setdefault(check.local_dir, []).append(check)

    for local_dir, local_checks in by_dir.items():
        output_path = Path(local_dir) / "SHA256SUMS"
        with output_path.open("w", encoding="utf-8") as handle:
            for check in local_checks:
                handle.write(f"{check.sha256_actual}  {check.path}\n")


def main() -> int:
    args = parse_args()
    all_checks: list[FileCheck] = []
    summaries: list[dict[str, Any]] = []
    ignore_extra = set(args.ignore_extra)
    ignore_official = set(args.ignore_official)
    ignore_extra.update(ignore_official)

    for spec in args.model:
        print(f"Verifying {spec.model_id} at {spec.local_dir}...", flush=True)
        checks, summary = verify_model(
            spec, args.revision, args.chunk_size, ignore_extra, ignore_official
        )
        all_checks.extend(checks)
        summaries.append(summary)
        print(
            f"  ok={summary['ok']} missing={summary['missing']} "
            f"size_mismatch={summary['size_mismatch']} "
            f"sha256_mismatch={summary['sha256_mismatch']} "
            f"extra={summary['extra_file_count']}",
            flush=True,
        )

    tsv_path, json_path, sha256sums_path = write_outputs(
        args.output_dir, args.output_prefix, all_checks, summaries
    )
    if args.write_model_sha256sums:
        write_model_sha256sums(all_checks)

    print(f"Wrote aggregate SHA256 sums: {sha256sums_path}")
    print(f"Wrote verification TSV     : {tsv_path}")
    print(f"Wrote verification JSON    : {json_path}")

    failed = any(
        summary["missing"] or summary["size_mismatch"] or summary["sha256_mismatch"]
        for summary in summaries
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
