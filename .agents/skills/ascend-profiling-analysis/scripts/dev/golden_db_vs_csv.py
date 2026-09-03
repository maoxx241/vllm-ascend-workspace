#!/usr/bin/env python3
"""Golden equivalence check: db-direct adapter output vs kernel_details.csv.

Compares ``sources_db.iter_kernel_events_from_db`` row-by-row,
field-by-field against the kernel_details.csv exported from the SAME
capture, over all rows (no sampling).  Designed to run on the remote NPU
container with stdlib + PyYAML only.

Field verdicts:
  * ``exact``    -- string-identical (identifiers, enums, shape cells,
                    cycle counters, Start/Duration ns-exact text).
  * ``float``    -- |adapter - csv| <= --tolerance (PMU times/ratios,
                    cube_utilization; CSV rounds to 3 decimals).
  * ``adopted``  -- fields whose CSV semantics cannot be bit-reproduced from
                    the db (see knowledge/db_source_mapping.yaml
                    ``documented_differences``); deviations are measured and
                    reported but do not fail the check:
                      - Wait Time(us): idle-gap vs exporter queue-wait.
                      - Block Num / Mix Block Num on AI_CPU rows: absent from
                        the db schema (adapter emits N/A).
  * everything else is an ``unexplained_mismatch`` and fails the check.

Row order: both streams are Start-Time sorted; exact-startNs tie groups are
compared as unordered multisets (the CSV export's tie order is not
reproducible — see the mapping file).

Exit code 0 iff row counts match, start-time sequences align, and there are
zero unexplained mismatches.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

csv.field_size_limit(1024 * 1024 * 1024)

EXACT_FIELDS = (
    "Device_id", "Model ID", "Task ID", "Stream ID", "Name", "Type", "OP State",
    "Accelerator Core", "HF32 Eligible", "Input Shapes", "Input Data Types",
    "Input Formats", "Output Shapes", "Output Data Types", "Output Formats",
    "Context ID", "Start Time(us)", "Duration(us)",
    "aic_total_cycles", "aiv_total_cycles",
)
FLOAT_FIELDS = (
    "aicore_time(us)", "aic_mac_time(us)", "aic_mac_ratio", "aic_scalar_time(us)",
    "aic_scalar_ratio", "aic_mte1_time(us)", "aic_mte1_ratio", "aic_mte2_time(us)",
    "aic_mte2_ratio", "aic_fixpipe_time(us)", "aic_fixpipe_ratio",
    "aic_icache_miss_rate", "aiv_time(us)", "aiv_vec_time(us)", "aiv_vec_ratio",
    "aiv_scalar_time(us)", "aiv_scalar_ratio", "aiv_mte2_time(us)", "aiv_mte2_ratio",
    "aiv_mte3_time(us)", "aiv_mte3_ratio", "aiv_icache_miss_rate",
    "cube_utilization(%)",
)
ADOPTED_FIELDS = ("Wait Time(us)", "Block Num", "Mix Block Num")
IDENTITY_FIELDS = ("Name", "Type", "Accelerator Core", "Task ID", "Stream ID")
NA = "N/A"


def us_text_to_ns(us_text: str) -> int:
    text = us_text.strip()
    int_part, _, frac = text.partition(".")
    return int(int_part) * 1000 + int((frac + "000")[:3])


def iter_csv_dicts(csv_path: Path) -> Iterator[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            yield row


def start_groups(stream: Iterable[Mapping[str, str]]) -> Iterator[tuple[int, list[Mapping[str, str]]]]:
    """Group consecutive rows sharing the exact same Start Time (ns)."""

    group: list[Mapping[str, str]] = []
    group_ns: int | None = None
    for row in stream:
        ns = us_text_to_ns(row["Start Time(us)"])
        if group_ns is None or ns == group_ns:
            group.append(row)
            group_ns = ns
            continue
        yield group_ns, group
        group, group_ns = [row], ns
    if group_ns is not None:
        yield group_ns, group


class FieldStats:
    def __init__(self) -> None:
        self.matched = 0
        self.mismatched = 0
        self.adopted_diff = 0
        self.max_abs_diff = 0.0
        self.examples: list[dict[str, Any]] = []

    def example(self, row_idx: int, csv_value: str, db_value: str) -> None:
        if len(self.examples) < 5:
            self.examples.append({"row_idx": row_idx, "csv": csv_value, "db": db_value})


def _try_float(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _try_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.strip().replace(",", ""))
    except (InvalidOperation, ValueError, AttributeError):
        return None


def compare_field(
    field: str,
    csv_value: str,
    db_value: str,
    stats: FieldStats,
    row_idx: int,
    *,
    tolerance: float,
    tolerance_d: Decimal,
    is_aicpu: bool,
) -> None:
    if field in ADOPTED_FIELDS:
        if is_aicpu or field == "Wait Time(us)":
            # Documented adoption: measure, never fail.  (aicpu Block/Mix Num
            # have no db source at all; Wait Time semantics differ by design.)
            if csv_value != db_value:
                stats.adopted_diff += 1
                csv_f, db_f = _try_float(csv_value), _try_float(db_value)
                if csv_f is not None and db_f is not None:
                    stats.max_abs_diff = max(stats.max_abs_diff, abs(csv_f - db_f))
                stats.example(row_idx, csv_value, db_value)
                return
        # Block Num / Mix Block Num on non-aicpu rows must match exactly.
    if field in FLOAT_FIELDS:
        if csv_value == db_value:
            stats.matched += 1
            return
        # Decimal comparison: both cells carry at most 3 decimals, so the
        # |a-b| <= 1e-3 boundary is exact decimal arithmetic — a binary
        # float subtraction would overshoot the tie by ~1e-16 and turn
        # rounding-boundary cases (csv 30.517 vs db 30.518) into failures.
        csv_d, db_d = _try_decimal(csv_value), _try_decimal(db_value)
        if csv_d is not None and db_d is not None:
            diff = abs(csv_d - db_d)
            if diff <= tolerance_d:
                stats.matched += 1
                stats.max_abs_diff = max(stats.max_abs_diff, float(diff))
                return
        stats.mismatched += 1
        stats.example(row_idx, csv_value, db_value)
        return
    # exact fields (Start Time trailing tab is a documented export artifact:
    # compare both sides stripped)
    left = csv_value.strip() if field == "Start Time(us)" else csv_value
    right = db_value.strip() if field == "Start Time(us)" else db_value
    if left == right:
        stats.matched += 1
    else:
        stats.mismatched += 1
        stats.example(row_idx, csv_value, db_value)


def compare_rows(csv_row: Mapping[str, str], db_row: Mapping[str, str], row_idx: int, stats: dict[str, FieldStats], *, tolerance: float, tolerance_d: Decimal) -> None:
    is_aicpu = db_row.get("Accelerator Core") == "AI_CPU"
    for field in EXACT_FIELDS + FLOAT_FIELDS + ADOPTED_FIELDS:
        compare_field(
            field,
            csv_row.get(field, NA),
            db_row.get(field, NA),
            stats[field],
            row_idx,
            tolerance=tolerance,
            tolerance_d=tolerance_d,
            is_aicpu=is_aicpu,
        )


def match_tie_group(
    csv_group: list[Mapping[str, str]],
    db_group: list[Mapping[str, str]],
    row_idx_base: int,
    stats: dict[str, FieldStats],
    *,
    tolerance: float,
    tolerance_d: Decimal,
) -> list[dict[str, Any]]:
    """Compare one start-time tie group as an unordered multiset."""

    unexplained: list[dict[str, Any]] = []
    remaining = list(db_group)
    pairs: list[tuple[int, Mapping[str, str], Mapping[str, str]]] = []
    for offset, csv_row in enumerate(csv_group):
        row_idx = row_idx_base + offset
        identity = tuple(csv_row.get(field) for field in IDENTITY_FIELDS)
        hit = None
        for candidate in remaining:
            if tuple(candidate.get(field) for field in IDENTITY_FIELDS) == identity:
                hit = candidate
                break
        if hit is None:
            unexplained.append({"row_idx": row_idx, "reason": "no db row with matching identity", "csv": dict(csv_row)})
        else:
            remaining.remove(hit)
            pairs.append((row_idx, csv_row, hit))
    for leftover in remaining:
        unexplained.append({"row_idx": row_idx_base, "reason": "db row without csv counterpart", "db": dict(leftover)})
    for row_idx, csv_row, db_row in pairs:
        compare_rows(csv_row, db_row, row_idx, stats, tolerance=tolerance, tolerance_d=tolerance_d)
    return unexplained


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--tolerance", type=float, default=1e-3)
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    args = parser.parse_args(argv)
    tolerance_d = Decimal(str(args.tolerance))

    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from ascend_profile.sources_db import iter_kernel_events_from_db  # noqa: E402

    fields = EXACT_FIELDS + FLOAT_FIELDS + ADOPTED_FIELDS
    stats = {field: FieldStats() for field in fields}
    unexplained: list[dict[str, Any]] = []
    row_count_csv = 0
    row_count_db = 0
    ordering_fatal = False

    started = time.monotonic()
    db_stream = iter_kernel_events_from_db(args.db)
    db_rows = (row for _row_idx, row in db_stream)
    adapter_elapsed_first_row: float | None = None

    csv_groups = start_groups(iter_csv_dicts(args.csv))
    db_groups = start_groups(db_rows)
    csv_group = next(csv_groups, None)
    db_group = next(db_groups, None)
    group_idx = 0
    while csv_group is not None and db_group is not None:
        if adapter_elapsed_first_row is None:
            adapter_elapsed_first_row = time.monotonic() - started
        csv_ns, csv_rows = csv_group
        db_ns, db_rows_group = db_group
        if csv_ns != db_ns:
            unexplained.append({
                "row_idx": row_count_csv,
                "reason": f"start-time divergence: csv group at {csv_ns}ns vs db group at {db_ns}ns",
            })
            ordering_fatal = True
            break
        row_count_csv += len(csv_rows)
        row_count_db += len(db_rows_group)
        unexplained.extend(match_tie_group(csv_rows, db_rows_group, row_count_csv - len(csv_rows), stats, tolerance=args.tolerance, tolerance_d=tolerance_d))
        group_idx += 1
        csv_group = next(csv_groups, None)
        db_group = next(db_groups, None)
    # one stream may end earlier: count and flag the leftover rows
    if csv_group is not None:
        row_count_csv += len(csv_group[1])
        ordering_fatal = True
        unexplained.append({"row_idx": row_count_csv, "reason": "csv has rows beyond the db stream"})
    if db_group is not None:
        row_count_db += len(db_group[1])
        ordering_fatal = True
        unexplained.append({"row_idx": row_count_db, "reason": "db stream has rows beyond the csv"})
    for _csv_ns, csv_rows in csv_groups:
        row_count_csv += len(csv_rows)
    for _db_ns, db_rows_group in db_groups:
        row_count_db += len(db_rows_group)
    elapsed = time.monotonic() - started

    total_mismatched = sum(item.mismatched for item in stats.values())
    report: dict[str, Any] = {
        "db": str(args.db),
        "csv": str(args.csv),
        "tolerance": args.tolerance,
        "elapsed_s": round(elapsed, 3),
        "adapter_first_group_s": round(adapter_elapsed_first_row or 0.0, 3),
        "rows": {"csv": row_count_csv, "db": row_count_db},
        "groups_compared": group_idx,
        "unexplained_mismatch_rows": len(unexplained),
        "unexplained_field_mismatches": total_mismatched,
        "ordering_fatal": ordering_fatal,
        "fields": {
            field: {
                "matched": item.matched,
                "mismatched": item.mismatched,
                "adopted_diff": item.adopted_diff,
                "max_abs_diff": round(item.max_abs_diff, 9),
                "examples": item.examples,
            }
            for field, item in stats.items()
            if item.mismatched or item.adopted_diff or field in ADOPTED_FIELDS
        },
        "unexplained_examples": unexplained[:10],
        "verdict": "PASS" if not unexplained and total_mismatched == 0 and row_count_csv == row_count_db else "FAIL",
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
