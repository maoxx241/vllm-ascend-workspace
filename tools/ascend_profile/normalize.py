#!/usr/bin/env python3
"""Normalize raw Ascend profiling files into event/source indexes."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Sequence

try:
    from .common import (
        NormalizedEvent,
        SCHEMA_VERSION,
        SourceRef,
        TOOL_VERSION,
        categories_and_roles,
        core_from_row,
        discover_rank_dirs,
        event_time_from_row,
        has_pipeline_signal,
        infer_rank_id,
        iter_csv_rows,
        kernel_details_path,
        name_from_row,
        op_type_from_event,
        pick,
        pipeline_breakdown_from_row,
        sha256_file,
        shape_signature,
        stable_id,
        stream_from_row,
        supplemental_sources,
        task_type_from_row,
        utc_now,
        to_plain,
        write_json,
    )
except ImportError:  # pragma: no cover - direct script execution
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import (  # type: ignore[no-redef]
        NormalizedEvent,
        SCHEMA_VERSION,
        SourceRef,
        TOOL_VERSION,
        categories_and_roles,
        core_from_row,
        discover_rank_dirs,
        event_time_from_row,
        has_pipeline_signal,
        infer_rank_id,
        iter_csv_rows,
        kernel_details_path,
        name_from_row,
        op_type_from_event,
        pick,
        pipeline_breakdown_from_row,
        sha256_file,
        shape_signature,
        stable_id,
        stream_from_row,
        supplemental_sources,
        task_type_from_row,
        utc_now,
        to_plain,
        write_json,
    )


def maybe_sha256(path: Path, enabled: bool) -> str | None:
    return sha256_file(path) if enabled else None


EVENT_FIELDNAMES = [
    "event_id",
    "profile_id",
    "rank_id",
    "source_id",
    "row_idx",
    "name_raw",
    "task_type",
    "accelerator_core",
    "stream_id",
    "start_us",
    "end_us",
    "duration_us",
    "wait_us",
    "op_categories",
    "op_roles",
    "shape_signature",
    "shape_features",
    "pipeline_us",
    "op_type",
]

_JSON_TEXT_CACHE: dict[tuple[Any, ...], str] = {}


def tuple_json_text(values: tuple[Any, ...]) -> str:
    cached = _JSON_TEXT_CACHE.get(values)
    if cached is None:
        cached = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))
        _JSON_TEXT_CACHE[values] = cached
    return cached


def event_csv_row(event: NormalizedEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "profile_id": event.profile_id,
        "rank_id": event.rank_id,
        "source_id": event.source_id,
        "row_idx": event.row_idx,
        "name_raw": event.name_raw,
        "task_type": event.task_type,
        "accelerator_core": event.accelerator_core,
        "stream_id": event.stream_id,
        "start_us": event.start_us,
        "end_us": event.end_us,
        "duration_us": event.duration_us,
        "wait_us": event.wait_us,
        "op_categories": tuple_json_text(event.op_categories),
        "op_roles": tuple_json_text(event.op_roles),
        "shape_signature": event.shape_signature or "",
        "shape_features": "{}" if not event.shape_features else json.dumps(event.shape_features, ensure_ascii=False, separators=(",", ":")),
        "pipeline_us": "{}" if not event.pipeline_us else json.dumps(event.pipeline_us, ensure_ascii=False, separators=(",", ":")),
        "op_type": event.op_type,
    }


def normalize_profile(
    profile_root: Path,
    output_dir: Path,
    *,
    hash_sources: bool = False,
    write_jsonl: bool = False,
) -> dict[str, Any]:
    profile_root = profile_root.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_id = stable_id("profile", profile_root)
    rank_dirs = discover_rank_dirs(profile_root)
    sources: list[SourceRef] = []
    rank_summaries: list[dict[str, Any]] = []
    event_count = 0
    event_path = output_dir / "normalized_event_index.csv"
    jsonl_path = output_dir / "normalized_event_index.jsonl"

    with event_path.open("w", encoding="utf-8", newline="") as event_handle:
        writer = csv.DictWriter(event_handle, fieldnames=EVENT_FIELDNAMES)
        writer.writeheader()
        jsonl_handle = jsonl_path.open("w", encoding="utf-8") if write_jsonl else None
        for ordinal, rank_dir in enumerate(rank_dirs):
            rank_id = infer_rank_id(rank_dir, ordinal)
            kernel_csv = kernel_details_path(rank_dir)
            if kernel_csv is None:
                continue
            source = SourceRef(
                source_id=stable_id("src", profile_id, rank_id, kernel_csv),
                kind="kernel_details_csv",
                path=str(kernel_csv),
                sha256=maybe_sha256(kernel_csv, hash_sources),
                rank_id=rank_id,
                row_start=0,
                row_end=None,
            )
            for kind, path in supplemental_sources(rank_dir):
                sources.append(
                    SourceRef(
                        source_id=stable_id("src", profile_id, rank_id, kind, path),
                        kind=kind,
                        path=str(path),
                        sha256=maybe_sha256(path, hash_sources),
                        rank_id=rank_id,
                        row_base="not_applicable" if kind != "op_summary_csv" else "zero_based",
                    )
                )
            rank_event_count = 0
            rank_pipeline_event_count = 0
            rank_start_us: float | None = None
            rank_end_us: float | None = None
            last_row_idx: int | None = None
            for row_idx, row in iter_csv_rows(kernel_csv):
                name = name_from_row(row)
                task = task_type_from_row(row)
                core = core_from_row(row)
                stream_id = stream_from_row(row)
                start_us, end_us, duration_us, wait_us = event_time_from_row(row)
                categories, roles = categories_and_roles(name, task, core)
                if set(roles).intersection({"attention", "moe", "compute", "communication"}):
                    shape_sig, _shape_features = shape_signature(row)
                else:
                    shape_sig = None
                shape_features: dict[str, Any] = {}
                pipeline_us = pipeline_breakdown_from_row(row)
                if has_pipeline_signal(pipeline_us):
                    rank_pipeline_event_count += 1
                event_op_type = op_type_from_event(core, pipeline_us)
                raw_ref = SourceRef(
                    source_id=source.source_id,
                    kind=source.kind,
                    path=source.path,
                    sha256=source.sha256,
                    rank_id=rank_id,
                    row_start=row_idx,
                    row_end=row_idx,
                )
                event = NormalizedEvent(
                    event_id=f"evt_{ordinal}_{row_idx}",
                    profile_id=profile_id,
                    rank_id=rank_id,
                    source_id=source.source_id,
                    row_idx=row_idx,
                    name_raw=name,
                    task_type=task,
                    accelerator_core=core,
                    stream_id=stream_id,
                    start_us=start_us,
                    end_us=end_us,
                    duration_us=duration_us,
                    wait_us=wait_us,
                    op_categories=categories,
                    op_roles=roles,
                    shape_signature=shape_sig,
                    shape_features=shape_features,
                    pipeline_us=pipeline_us,
                    op_type=event_op_type,
                    raw_fields_ref=raw_ref,
                )
                writer.writerow(event_csv_row(event))
                if jsonl_handle is not None:
                    jsonl_handle.write(json.dumps(to_plain(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                rank_event_count += 1
                event_count += 1
                rank_start_us = start_us if rank_start_us is None else min(rank_start_us, start_us)
                rank_end_us = end_us if rank_end_us is None else max(rank_end_us, end_us)
                last_row_idx = row_idx
            source = SourceRef(
                source_id=source.source_id,
                kind=source.kind,
                path=source.path,
                sha256=source.sha256,
                rank_id=rank_id,
                row_start=0,
                row_end=last_row_idx,
            )
            sources.append(source)
            rank_summaries.append(
                {
                    "rank_id": rank_id,
                    "rank_dir": str(rank_dir),
                    "kernel_details_csv": str(kernel_csv),
                    "event_count": rank_event_count,
                    "row_count": rank_event_count,
                    "start_us": rank_start_us,
                    "end_us": rank_end_us,
                    "source_id": source.source_id,
                    "pipeline_event_count": rank_pipeline_event_count,
                    "pipeline_coverage": (
                        round(rank_pipeline_event_count / rank_event_count, 6)
                        if rank_event_count
                        else 0.0
                    ),
                }
            )
        if jsonl_handle is not None:
            jsonl_handle.close()

    pipeline_event_count = sum(int(item.get("pipeline_event_count") or 0) for item in rank_summaries)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "analysis_stage": "normalize",
        "created_at": utc_now(),
        "profile_id": profile_id,
        "profile_root": str(profile_root),
        "output_dir": str(output_dir),
        "rank_count": len(rank_summaries),
        "event_count": event_count,
        "pipeline_event_count": pipeline_event_count,
        "pipeline_coverage": round(pipeline_event_count / event_count, 6) if event_count else 0.0,
        "hash_sources": hash_sources,
        "write_jsonl": write_jsonl,
        "files": {
            "normalized_event_index": "normalized_event_index.csv",
            "normalized_event_index_jsonl": "normalized_event_index.jsonl" if write_jsonl else None,
            "source_index": "source_index.json",
            "normalize_manifest": "normalize_manifest.json",
        },
        "rank_summaries": rank_summaries,
    }
    write_json(output_dir / "source_index.json", {"sources": sources})
    write_json(output_dir / "normalize_manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile_root")
    parser.add_argument("--output", required=True)
    parser.add_argument("--hash-sources", action="store_true")
    parser.add_argument("--write-jsonl", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = normalize_profile(
        Path(args.profile_root),
        Path(args.output),
        hash_sources=bool(args.hash_sources),
        write_jsonl=bool(args.write_jsonl),
    )
    print(
        {
            "stage": "normalize",
            "rank_count": manifest["rank_count"],
            "event_count": manifest["event_count"],
            "pipeline_coverage": manifest.get("pipeline_coverage"),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
