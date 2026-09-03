#!/usr/bin/env python3
"""Normalize raw Ascend profiling files into event/source indexes."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

try:
    from .common import (
        NormalizedEvent,
        PIPELINE_FIELDS,
        SCHEMA_VERSION,
        SourceRef,
        TOOL_VERSION,
        categories_and_roles,
        discover_rank_dirs,
        has_pipeline_signal,
        infer_rank_id,
        kernel_db_path,
        kernel_details_path,
        op_type_from_event,
        sha256_file,
        stable_id,
        supplemental_sources,
        utc_now,
        to_plain,
        write_json,
    )
    from .sources import KernelRowAccessor, shape_signature_from_text
    from .sources_db import db_row_reader, probe_db_schema
    from .work import estimated_work_from_fields
except ImportError:  # pragma: no cover - direct script execution
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import (  # type: ignore[no-redef]
        NormalizedEvent,
        PIPELINE_FIELDS,
        SCHEMA_VERSION,
        SourceRef,
        TOOL_VERSION,
        categories_and_roles,
        discover_rank_dirs,
        has_pipeline_signal,
        infer_rank_id,
        kernel_db_path,
        kernel_details_path,
        op_type_from_event,
        sha256_file,
        stable_id,
        supplemental_sources,
        utc_now,
        to_plain,
        write_json,
    )
    from sources import KernelRowAccessor, shape_signature_from_text  # type: ignore[no-redef]
    from sources_db import db_row_reader, probe_db_schema  # type: ignore[no-redef]
    from work import estimated_work_from_fields  # type: ignore[no-redef]


def maybe_sha256(path: Path, enabled: bool) -> str | None:
    return sha256_file(path) if enabled else None


SOURCE_CHOICES = ("auto", "db", "csv")
SOURCE_KIND_CSV = "kernel_details_csv"
SOURCE_KIND_DB = "kernel_details_db"


def _probe_summary(probe: Mapping[str, Any]) -> str:
    if probe.get("error"):
        return str(probe["error"])
    missing = probe.get("missing") or {}
    return "missing " + ", ".join(f"{table}({','.join(cols)})" for table, cols in sorted(missing.items()))


def _resolve_rank_source(
    rank_dir: Path,
    mode: str,
) -> tuple[Path | None, Path | None, str | None, str | None]:
    """Pick the kernel event source for one rank directory.

    Returns ``(kernel_csv, kernel_db, source_kind, note)``.  ``source_kind``
    is None when no usable source exists (the rank is skipped, mirroring the
    historical missing-csv behaviour); ``note`` carries a human-readable
    reason for fallbacks and skips, recorded in the manifest.

    ``auto`` prefers the profiler db whenever it exists and passes the
    schema probe (fail-closed), and falls back to kernel_details.csv.
    """

    kernel_csv = kernel_details_path(rank_dir)
    kernel_db = kernel_db_path(rank_dir)
    if mode == "csv":
        if kernel_csv is None:
            return kernel_csv, kernel_db, None, "no kernel_details.csv found (--source csv)"
        return kernel_csv, kernel_db, SOURCE_KIND_CSV, None
    if mode == "db":
        if kernel_db is None:
            return kernel_csv, kernel_db, None, "no ascend_pytorch_profiler_*.db found (--source db)"
        probe = probe_db_schema(kernel_db)
        if not probe["ok"]:
            return kernel_csv, kernel_db, None, f"db schema probe failed: {_probe_summary(probe)}"
        note = None
        optional_missing = probe.get("optional_missing") or {}
        if optional_missing:
            note = "db lacks optional tables (no comm rows contributed): " + ", ".join(sorted(optional_missing))
        return kernel_csv, kernel_db, SOURCE_KIND_DB, note
    # auto
    if kernel_db is not None:
        probe = probe_db_schema(kernel_db)
        if probe["ok"]:
            note = None
            optional_missing = probe.get("optional_missing") or {}
            if optional_missing:
                note = "db lacks optional tables (no comm rows contributed): " + ", ".join(sorted(optional_missing))
            return kernel_csv, kernel_db, SOURCE_KIND_DB, note
        note = f"db schema probe failed ({_probe_summary(probe)})"
        if kernel_csv is not None:
            return kernel_csv, kernel_db, SOURCE_KIND_CSV, note + "; fell back to kernel_details.csv"
        return kernel_csv, kernel_db, None, note + "; no kernel_details.csv fallback"
    if kernel_csv is not None:
        return kernel_csv, kernel_db, SOURCE_KIND_CSV, None
    return kernel_csv, kernel_db, None, None


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

# Roles that make an event worth a ``shape_signature`` (mirrors the
# historical ``set(roles).intersection({...})`` check in the row loop).
_SHAPE_RELEVANT_ROLES = frozenset({"attention", "moe", "compute", "communication"})


def tuple_json_text(values: tuple[Any, ...]) -> str:
    cached = _JSON_TEXT_CACHE.get(values)
    if cached is None:
        cached = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))
        _JSON_TEXT_CACHE[values] = cached
    return cached


# Pre-serialized ``"key":`` fragments for ``pipeline_json_text``.  Keys come
# from the fixed pipeline schema, so the quoted form never changes;
# precomputing shaves per-row f-string overhead.
_PIPELINE_KEY_PREFIX: dict[str, str] = {key: f'"{key}":' for key in PIPELINE_FIELDS}


def pipeline_json_text(pipeline_us: Mapping[str, float]) -> str:
    """Byte-identical fast path for ``json.dumps(pipeline_us, separators=(",", ":"))``.

    Keys are the fixed ``PIPELINE_FIELDS`` identifiers (``[a-z0-9_]`` only,
    so no JSON escaping is needed) and values are finite floats, which the
    JSON encoder renders with ``float.__repr__`` — the same ``repr`` used
    here.
    """

    if not pipeline_us:
        return "{}"
    prefixes = _PIPELINE_KEY_PREFIX
    return "{" + ",".join(prefixes[key] + repr(value) for key, value in pipeline_us.items()) + "}"


def _kernel_row_reader(kernel_csv: Path) -> tuple[KernelRowAccessor, Iterator[tuple[int, list[str]]]]:
    """Open a kernel_details.csv for positional row iteration.

    Returns the header-resolved accessor plus a ``(row_idx, fields)``
    iterator over data rows (zero-based, matching ``store.iter_csv_rows``).
    The file handle travels with the iterator and closes when it is
    exhausted.
    """

    handle = kernel_csv.open("r", encoding="utf-8-sig", newline="")
    reader = csv.reader(handle)
    header = next(reader, None) or []

    def rows() -> Iterator[tuple[int, list[str]]]:
        try:
            for row_idx, fields in enumerate(reader):
                yield row_idx, fields
        finally:
            handle.close()

    return KernelRowAccessor(header), rows()


def event_csv_fields(
    event: NormalizedEvent,
    *,
    shape_features_json: str | None = None,
    pipeline_us_json: str | None = None,
) -> tuple[Any, ...]:
    """Event row as a tuple in ``EVENT_FIELDNAMES`` order.

    ``shape_features_json`` / ``pipeline_us_json`` let hot loops pass
    pre-serialized (and cached) JSON text; when omitted the dict is dumped
    here, identical to ``event_csv_row``.
    """

    if shape_features_json is None:
        shape_features_json = "{}" if not event.shape_features else json.dumps(event.shape_features, ensure_ascii=False, separators=(",", ":"))
    if pipeline_us_json is None:
        pipeline_us_json = "{}" if not event.pipeline_us else json.dumps(event.pipeline_us, ensure_ascii=False, separators=(",", ":"))
    return (
        event.event_id,
        event.profile_id,
        event.rank_id,
        event.source_id,
        event.row_idx,
        event.name_raw,
        event.task_type,
        event.accelerator_core,
        event.stream_id,
        event.start_us,
        event.end_us,
        event.duration_us,
        event.wait_us,
        tuple_json_text(event.op_categories),
        tuple_json_text(event.op_roles),
        event.shape_signature or "",
        shape_features_json,
        pipeline_us_json,
        event.op_type,
    )


def event_csv_row(
    event: NormalizedEvent,
    *,
    shape_features_json: str | None = None,
    pipeline_us_json: str | None = None,
) -> dict[str, Any]:
    return dict(zip(EVENT_FIELDNAMES, event_csv_fields(event, shape_features_json=shape_features_json, pipeline_us_json=pipeline_us_json)))


def normalize_profile(
    profile_root: Path,
    output_dir: Path,
    *,
    hash_sources: bool = False,
    write_jsonl: bool = False,
    source: str = "auto",
) -> tuple[list[NormalizedEvent], dict[str, Any]]:
    """Normalize raw profiling files; return ``(events, manifest)``.

    The in-memory event list is the same data written to
    ``normalized_event_index.csv`` (same row order); the full-pipeline
    runner hands it to downstream stages so they don't re-parse the CSV.
    Only the manifest is persisted to ``stage_results`` / JSON.

    ``source`` selects the kernel event input per rank: ``auto`` prefers the
    profiler sqlite db when present and probe-clean (falling back to
    kernel_details.csv), ``db``/``csv`` force one input.  Both sources feed
    the identical row loop below; the db adapter reproduces the
    kernel_details.csv column layout, so downstream stages see no difference.
    """
    if source not in SOURCE_CHOICES:
        raise ValueError(f"unknown source mode {source!r}; expected one of {SOURCE_CHOICES}")
    source_mode = source  # the rank loop reuses the name ``source`` for SourceRef
    profile_root = profile_root.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_id = stable_id("profile", profile_root)
    rank_dirs = discover_rank_dirs(profile_root)
    sources: list[SourceRef] = []
    source_notes: list[str] = []
    rank_summaries: list[dict[str, Any]] = []
    events: list[NormalizedEvent] = []
    event_count = 0
    event_path = output_dir / "normalized_event_index.csv"
    jsonl_path = output_dir / "normalized_event_index.jsonl"

    with event_path.open("w", encoding="utf-8", newline="") as event_handle:
        writer = csv.writer(event_handle)
        writer.writerow(EVENT_FIELDNAMES)
        jsonl_handle = jsonl_path.open("w", encoding="utf-8") if write_jsonl else None
        for ordinal, rank_dir in enumerate(rank_dirs):
            rank_id = infer_rank_id(rank_dir, ordinal)
            kernel_csv, kernel_db, source_kind, source_note = _resolve_rank_source(rank_dir, source_mode)
            if source_note:
                source_notes.append(f"{rank_id}: {source_note}")
            if source_kind is None:
                continue
            source_path = kernel_csv if source_kind == SOURCE_KIND_CSV else kernel_db
            source = SourceRef(
                source_id=stable_id("src", profile_id, rank_id, source_path),
                kind=source_kind,
                path=str(source_path),
                sha256=maybe_sha256(source_path, hash_sources),
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
            # The work estimate depends only on (name, task_type, op_type)
            # plus the four raw shape/dtype cells, so memoize on those raw
            # strings and skip shape/dtype parsing for repeated combos. The
            # serialized shape_features JSON text is cached on the same key.
            work_estimate_cache: dict[tuple[str, ...], dict[str, Any]] = {}
            shape_features_json_cache: dict[tuple[str, ...], str] = {}
            if source_kind == SOURCE_KIND_DB:
                accessor, kernel_rows = db_row_reader(kernel_db)
            else:
                accessor, kernel_rows = _kernel_row_reader(kernel_csv)
            for row_idx, row in kernel_rows:
                name = accessor.name(row)
                task = accessor.task_type(row)
                core = accessor.core(row)
                stream_id = accessor.stream(row)
                start_us, end_us, duration_us, wait_us = accessor.event_time(row)
                categories, roles = categories_and_roles(name, task, core)
                pipeline_us = accessor.pipeline_breakdown(row)
                event_op_type = op_type_from_event(core, pipeline_us)
                if not _SHAPE_RELEVANT_ROLES.isdisjoint(roles):
                    shape_sig = shape_signature_from_text(accessor.shape_text(row))[0]
                else:
                    shape_sig = None
                work_key = (name, task, event_op_type, *accessor.work_fields(row))
                shape_features = work_estimate_cache.get(work_key)
                if shape_features is None:
                    shape_features = estimated_work_from_fields(
                        name=name,
                        task_type=task,
                        op_type=event_op_type,
                        input_shapes_raw=work_key[3],
                        output_shapes_raw=work_key[4],
                        input_dtypes_raw=work_key[5],
                        output_dtypes_raw=work_key[6],
                    )
                    work_estimate_cache[work_key] = shape_features
                if has_pipeline_signal(pipeline_us):
                    rank_pipeline_event_count += 1
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
                shape_features_json = shape_features_json_cache.get(work_key)
                if shape_features_json is None:
                    shape_features_json = json.dumps(shape_features, ensure_ascii=False, separators=(",", ":"))
                    shape_features_json_cache[work_key] = shape_features_json
                pipeline_us_json = pipeline_json_text(pipeline_us)
                writer.writerow(event_csv_fields(event, shape_features_json=shape_features_json, pipeline_us_json=pipeline_us_json))
                if jsonl_handle is not None:
                    jsonl_handle.write(json.dumps(to_plain(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                events.append(event)
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
                    "kernel_details_csv": str(kernel_csv) if kernel_csv else None,
                    "kernel_details_db": str(kernel_db) if kernel_db else None,
                    "source_kind": source_kind,
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
        "source": source_mode,
        "source_kinds": {item["rank_id"]: item["source_kind"] for item in rank_summaries},
        "source_notes": source_notes,
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
    return events, manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("profile_root")
    parser.add_argument("--output", required=True)
    parser.add_argument("--hash-sources", action="store_true")
    parser.add_argument("--write-jsonl", action="store_true")
    parser.add_argument(
        "--source",
        choices=SOURCE_CHOICES,
        default="auto",
        help="kernel event input: auto = profiler db when present and probe-clean, else kernel_details.csv",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _events, manifest = normalize_profile(
        Path(args.profile_root),
        Path(args.output),
        hash_sources=bool(args.hash_sources),
        write_jsonl=bool(args.write_jsonl),
        source=str(args.source),
    )
    print(
        {
            "stage": "normalize",
            "rank_count": manifest["rank_count"],
            "event_count": manifest["event_count"],
            "pipeline_coverage": manifest.get("pipeline_coverage"),
            "source_kinds": manifest.get("source_kinds"),
            "source_notes": manifest.get("source_notes"),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
