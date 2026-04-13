#!/usr/bin/env python3
"""
Direct SQLite reader for msprof profiling databases.

This script is uploaded to and executed on the remote NPU machine.  It
probes PROF_* directories for SQLite .db files, discovers their table
schemas, and extracts the memory-related tables (npu_module_mem,
npu_mem, memory_record) WITHOUT running ``msprof --export``.

Output: JSON on stdout with the extracted data and metadata.

Usage (on remote):
    python3 db_reader.py <msprof_output_dir> [--tables npu_module_mem,npu_mem]

Exit codes:
    0  – success, JSON on stdout
    1  – no PROF directories found
    2  – no .db files found
    3  – no relevant tables found in any .db
    4  – sqlite3 import failed or other error
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
from pathlib import Path


# ── Tables we care about for memory profiling ──────────────────────────
# These are the table-name prefixes msprof uses.  The actual table name
# may include a numeric suffix (e.g. npu_module_mem_0).
_TARGET_TABLE_PREFIXES = (
    "npu_module_mem",
    "npu_mem",
    "memory_record",
    "operator_memory",
)

# Column aliases: msprof .db files may use slightly different column
# names than the exported CSV files.  We normalise to the CSV names so
# the downstream analyzer (mem_analyze.py) can consume both sources
# identically.
_COLUMN_ALIASES: dict[str, dict[str, str]] = {
    "npu_module_mem": {
        # DB column → CSV column
        "device_id": "Device_id",
        "component": "Component",
        "timestamp_us": "Timestamp(us)",
        "timestamp(us)": "Timestamp(us)",
        "total_reserved_kb": "Total Reserved(KB)",
        "total_reserved(kb)": "Total Reserved(KB)",
        "total_reserved_mb": "Total Reserved(MB)",
        "total_reserved(mb)": "Total Reserved(MB)",
        "device": "Device",
    },
    "npu_mem": {
        "device_id": "Device_id",
        "event": "event",
        "ddr_kb": "ddr(KB)",
        "ddr(kb)": "ddr(KB)",
        "hbm_kb": "hbm(KB)",
        "hbm(kb)": "hbm(KB)",
        "memory_kb": "memory(KB)",
        "memory(kb)": "memory(KB)",
        "timestamp_us": "timestamp(us)",
        "timestamp(us)": "timestamp(us)",
    },
}


def _is_sqlite(path: str) -> bool:
    """Quick check: does file start with the SQLite magic bytes?"""
    try:
        with open(path, "rb") as f:
            return f.read(16).startswith(b"SQLite format 3")
    except (OSError, PermissionError):
        return False


def _find_db_files(msprof_dir: str) -> list[dict]:
    """Walk PROF_* directories looking for .db files.

    Returns a list of dicts: {path, prof_dir, device_id (or None)}.
    """
    results: list[dict] = []
    # Pattern 1: PROF_*/device_N/*.db
    # Pattern 2: PROF_*/host/*.db
    # Pattern 3: PROF_*/*.db (flat)
    for prof in sorted(glob.glob(os.path.join(msprof_dir, "PROF_*"))):
        if not os.path.isdir(prof):
            continue
        prof_name = os.path.basename(prof)

        # Check device_N subdirectories
        for dev_dir in sorted(glob.glob(os.path.join(prof, "device_*"))):
            if not os.path.isdir(dev_dir):
                continue
            dev_name = os.path.basename(dev_dir)
            try:
                dev_id = int(dev_name.split("_", 1)[1])
            except (IndexError, ValueError):
                dev_id = None

            for db_file in _find_db_in_dir(dev_dir):
                results.append({
                    "path": db_file,
                    "prof_dir": prof_name,
                    "device_id": dev_id,
                })

        # Check host subdirectory
        host_dir = os.path.join(prof, "host")
        if os.path.isdir(host_dir):
            for db_file in _find_db_in_dir(host_dir):
                results.append({
                    "path": db_file,
                    "prof_dir": prof_name,
                    "device_id": None,
                })

        # Check flat .db files directly in PROF_*
        for db_file in _find_db_in_dir(prof):
            results.append({
                "path": db_file,
                "prof_dir": prof_name,
                "device_id": None,
            })

    return results


def _find_db_in_dir(directory: str) -> list[str]:
    """Find all SQLite .db files in a single directory (non-recursive)."""
    found = []
    try:
        for name in os.listdir(directory):
            full = os.path.join(directory, name)
            if name.endswith(".db") and os.path.isfile(full) and _is_sqlite(full):
                found.append(full)
    except (OSError, PermissionError):
        pass
    return sorted(found)


def _list_tables(conn: sqlite3.Connection) -> list[str]:
    """List all user tables in a SQLite database."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    return [row[0] for row in cur.fetchall()]


def _match_target_table(table_name: str) -> str | None:
    """Return the target prefix if *table_name* matches one, else None."""
    lower = table_name.lower()
    for prefix in _TARGET_TABLE_PREFIXES:
        if lower.startswith(prefix):
            return prefix
    return None


def _normalise_columns(
    rows: list[dict],
    target: str,
) -> list[dict]:
    """Rename DB columns to CSV-compatible names."""
    aliases = _COLUMN_ALIASES.get(target, {})
    if not aliases:
        return rows
    out = []
    for row in rows:
        new_row = {}
        for k, v in row.items():
            mapped = aliases.get(k.lower(), k)
            new_row[mapped] = v
        out.append(new_row)
    return out


def _read_table(
    conn: sqlite3.Connection,
    table_name: str,
    target: str,
) -> list[dict]:
    """Read all rows from *table_name*, normalise columns."""
    try:
        cur = conn.execute(f"SELECT * FROM [{table_name}]")
        cols = [desc[0] for desc in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        return _normalise_columns(rows, target)
    except sqlite3.Error:
        return []


def _extract_from_db(
    db_path: str,
    wanted_tables: set[str] | None,
) -> dict:
    """Open one .db file and extract all matching tables.

    Returns:
        {
            "tables_found": [...],
            "all_tables": [...],
            "data": {target_prefix: [rows...]},
            "schema": {table_name: "CREATE TABLE ..."},
        }
    """
    result: dict = {
        "tables_found": [],
        "all_tables": [],
        "data": {},
        "schema": {},
    }
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        result["error"] = str(e)
        return result

    try:
        all_tables = _list_tables(conn)
        result["all_tables"] = all_tables

        for tbl in all_tables:
            target = _match_target_table(tbl)
            if target is None:
                continue
            if wanted_tables and target not in wanted_tables:
                continue

            rows = _read_table(conn, tbl, target)
            if rows:
                result["tables_found"].append(tbl)
                # Use the target prefix as the key (merge if multiple
                # tables share the same prefix, e.g. npu_module_mem_0
                # and npu_module_mem_1).
                result["data"].setdefault(target, []).extend(rows)

            # Capture schema for diagnostics
            try:
                cur = conn.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name=?",
                    (tbl,),
                )
                row = cur.fetchone()
                if row:
                    result["schema"][tbl] = row[0]
            except sqlite3.Error:
                pass
    finally:
        conn.close()

    return result


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(
        description="Read msprof .db files directly (no export needed)",
    )
    p.add_argument("msprof_dir", help="Path to msprof output directory")
    p.add_argument(
        "--tables",
        default="npu_module_mem,npu_mem",
        help="Comma-separated table prefixes to extract "
             "(default: npu_module_mem,npu_mem)",
    )
    args = p.parse_args()

    wanted = set(args.tables.split(",")) if args.tables else None

    # 1. Find PROF directories
    msprof_dir = args.msprof_dir
    if not os.path.isdir(msprof_dir):
        json.dump({"error": f"Directory not found: {msprof_dir}"}, sys.stdout)
        sys.exit(1)

    # 2. Find .db files
    db_files = _find_db_files(msprof_dir)
    if not db_files:
        json.dump({
            "error": "no_db_files",
            "msg": f"No SQLite .db files found under {msprof_dir}",
            "searched": msprof_dir,
        }, sys.stdout)
        sys.exit(2)

    # 3. Extract data from each .db
    per_db_results: list[dict] = []
    all_data: dict[str, list[dict]] = {}
    all_schemas: dict[str, str] = {}
    prof_device_map: dict[str, list[int]] = {}  # prof_name → [device_ids]

    for db_info in db_files:
        extracted = _extract_from_db(db_info["path"], wanted)
        extracted["db_path"] = db_info["path"]
        extracted["prof_dir"] = db_info["prof_dir"]
        extracted["device_id"] = db_info["device_id"]
        per_db_results.append(extracted)

        # Merge data
        for target, rows in extracted.get("data", {}).items():
            all_data.setdefault(target, []).extend(rows)

        # Track which devices each PROF covers
        prof_name = db_info["prof_dir"]
        dev_id = db_info["device_id"]
        if dev_id is not None:
            prof_device_map.setdefault(prof_name, [])
            if dev_id not in prof_device_map[prof_name]:
                prof_device_map[prof_name].append(dev_id)

        all_schemas.update(extracted.get("schema", {}))

    # Check if we found anything useful
    total_rows = sum(len(rows) for rows in all_data.values())
    if total_rows == 0:
        json.dump({
            "error": "no_relevant_tables",
            "msg": "Found .db files but no matching memory tables",
            "db_files": [d["path"] for d in db_files],
            "all_tables_seen": list({
                t for r in per_db_results for t in r.get("all_tables", [])
            }),
        }, sys.stdout)
        sys.exit(3)

    # 4. Output
    output = {
        "status": "ok",
        "source": "direct_db_read",
        "msprof_dir": msprof_dir,
        "db_files_found": len(db_files),
        "total_rows": total_rows,
        "tables_extracted": sorted(all_data.keys()),
        "prof_device_map": {
            k: sorted(v) for k, v in prof_device_map.items()
        },
        "schemas": all_schemas,
        "data": all_data,
        "per_db_detail": [
            {
                "db_path": r["db_path"],
                "prof_dir": r["prof_dir"],
                "device_id": r["device_id"],
                "tables_found": r["tables_found"],
                "all_tables": r["all_tables"],
                "error": r.get("error"),
            }
            for r in per_db_results
        ],
    }
    json.dump(output, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
