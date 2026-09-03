#!/usr/bin/env python3
"""Artifact IO and cell-level value coercion for the analysis framework.

Owns: schema/tool version constants, JSON / JSONL / CSV / XLSX read-write
helpers, id/time helpers, and the small scalar coercion helpers
(``to_float`` / ``to_int`` / ``norm_text`` / ``first_present`` /
``text_config`` / ``parse_jsonish``) shared by the report-side modules.
No profiling semantics live here.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from xml.sax.saxutils import escape


SCHEMA_VERSION = 1
TOOL_VERSION = "ascend-profile-analysis-0.1"
SPREADSHEET_COLUMN_BASE = 26
csv.field_size_limit(1024 * 1024 * 1024)

# Knowledge files shipped inside the package (synced to the remote
# container together with the code). Single definition; the stage modules
# import it from here instead of recomputing the path.
KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_id(prefix: str, *parts: Any, length: int = 16) -> str:
    text = "\x1f".join(str(part) for part in parts)
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()[:length]
    return f"{prefix}_{digest}"


def to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    return value


# Primitive fast path shared by csv_value / write_jsonl: these types pass
# through to_plain unchanged, so the dataclass/Mapping checks are pure cost.
_PLAIN_PASSTHROUGH = (str, int, float, bool, type(None))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_plain(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def emit_stage_json(payload: dict[str, Any]) -> None:
    """Emit a stage CLI's summary JSON to stdout, terminated by a newline.

    Callers (analyze/segment/classify/summarize/cross_rank/diagnostics/report)
    should funnel their final printout through this helper so wrappers and
    automation can consume valid JSON instead of Python dict repr.
    """
    import sys as _sys
    _sys.stdout.write(json.dumps(to_plain(payload), ensure_ascii=False) + "\n")
    _sys.stdout.flush()


def write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(to_plain(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(str(key))
                fieldnames.append(str(key))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in fieldnames})


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def iter_csv_rows(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_idx, row in enumerate(reader):
            yield row_idx, row


def csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def csv_value(value: Any) -> Any:
    if isinstance(value, _PLAIN_PASSTHROUGH):
        return value
    value = to_plain(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def sha256_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(block_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


_PICK_KEY_CACHE: dict[tuple[str, ...], dict[str, str]] = {}


def row_key_lookup(row_keys: tuple[str, ...]) -> dict[str, str]:
    """Cached lowercase→actual column-name map for a row's key tuple."""

    lowered = _PICK_KEY_CACHE.get(row_keys)
    if lowered is None:
        lowered = {key.strip().lower(): key for key in row_keys}
        _PICK_KEY_CACHE[row_keys] = lowered
    return lowered


def pick(row: Mapping[str, Any], keys: Sequence[str], default: str = "") -> str:
    lowered = row_key_lookup(tuple(str(key) for key in row.keys()))
    for key in keys:
        actual = lowered.get(key.strip().lower())
        if actual is None:
            continue
        value = str(row.get(actual, "")).strip()
        if value:
            return value
    return default


def resolve_pick_keys(row_keys: Iterable[str], keys: Sequence[str]) -> tuple[str, ...]:
    """Resolve ``pick`` candidate aliases to the row's actual column names.

    The candidate→column mapping is fixed for a whole CSV file (all
    ``csv.DictReader`` rows share the header), so hot loops can resolve
    once from the header instead of letting ``pick`` re-fold every
    candidate against the row key set on each call.  The result preserves
    candidate order, drops aliases absent from the row, and de-duplicates
    aliases that fold to the same actual column (a repeated lookup of one
    column can never change the outcome, so this stays exactly equivalent
    to calling ``pick`` with the original candidates).
    """

    lowered = row_key_lookup(tuple(str(key) for key in row_keys))
    resolved: list[str] = []
    seen: set[str] = set()
    for key in keys:
        actual = lowered.get(key.strip().lower())
        if actual is None or actual in seen:
            continue
        seen.add(actual)
        resolved.append(actual)
    return tuple(resolved)


def pick_resolved(row: Mapping[str, Any], resolved_keys: Sequence[str], default: str = "") -> str:
    """``pick`` against keys pre-resolved by ``resolve_pick_keys``.

    Same semantics as ``pick``: first column with a non-empty stripped
    value wins, ``default`` when none qualifies.
    """

    for actual in resolved_keys:
        value = str(row.get(actual, "")).strip()
        if value:
            return value
    return default


def resolve_pick_positions(header: Sequence[str], keys: Sequence[str]) -> tuple[int, ...]:
    """Positional twin of ``resolve_pick_keys`` for ``csv.reader`` rows.

    Resolves candidate aliases to column *indices* into ``header``.  When
    several header cells fold to the same lowercase name the LAST one wins,
    mirroring ``row_key_lookup`` (and therefore ``csv.DictReader``, where a
    later duplicate column overwrites the earlier value).
    """

    lowered: dict[str, int] = {}
    for idx, key in enumerate(header):
        lowered[str(key).strip().lower()] = idx
    resolved: list[int] = []
    seen: set[int] = set()
    for key in keys:
        pos = lowered.get(key.strip().lower())
        if pos is None or pos in seen:
            continue
        seen.add(pos)
        resolved.append(pos)
    return tuple(resolved)


def pick_at(row: Sequence[str], positions: Sequence[int], default: str = "") -> str:
    """``pick_resolved`` for positional (``csv.reader``) rows.

    Short-row parity with ``csv.DictReader`` + ``pick``: a missing trailing
    cell surfaces as ``None`` there, which ``pick`` stringifies to the
    literal ``"None"``; we reproduce that instead of silently defaulting.
    """

    row_len = len(row)
    for pos in positions:
        value = row[pos] if pos < row_len else None
        text = str(value).strip()
        if text:
            return text
    return default


def try_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null"}:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def fold_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


# ----------------------------------------------------------------------------
# Shared scalar / mapping coercion helpers
# ----------------------------------------------------------------------------
# These used to be re-defined per consumer (``_f`` / ``_i`` / ``_norm`` /
# ``_first`` / ``_text_config`` / ``parse_jsonish`` in report.py, sweep.py,
# diagnostics.py, model_context.py, model_insights.py, hardware_insights.py).
# Single home now; import from here.


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def norm_text(value: Any) -> str:
    """None-safe ``fold_text`` over ``str(value)``."""

    return fold_text(str(value or ""))


def first_present(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


def text_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    text = config.get("text_config")
    return text if isinstance(text, Mapping) else config


def parse_jsonish(value: Any, default: Any) -> Any:
    """Parse a JSON-encoded CSV cell; return ``default`` on blank/garbage."""

    if value is None:
        return default
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def write_xlsx(path: Path, sheets: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    """Write a minimal XLSX workbook using only the standard library."""

    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_items = [(safe_sheet_name(name), list(rows)) for name, rows in sheets.items()]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
""" + "".join(
                f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for idx, _ in enumerate(sheet_items, 1)
            ) + "\n</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
""" + "".join(
                f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>'
                for idx, _ in enumerate(sheet_items, 1)
            ) + f'<Relationship Id="rId{len(sheet_items)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            + "\n</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>
""" + "".join(
                f'<sheet name="{escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>'
                for idx, (name, _) in enumerate(sheet_items, 1)
            ) + "</sheets></workbook>",
        )
        zf.writestr(
            "xl/styles.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>""",
        )
        for idx, (_, rows) in enumerate(sheet_items, 1):
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", sheet_xml(rows))


def safe_sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", name)[:31]
    return cleaned or "Sheet"


def sheet_xml(rows: Sequence[Mapping[str, Any]]) -> str:
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(str(key))
                fieldnames.append(str(key))
    if not fieldnames:
        fieldnames = ["empty"]
    table = [dict(zip(fieldnames, fieldnames))]
    table.extend({key: row.get(key, "") for key in fieldnames} for row in rows)
    xml_rows = []
    for r_idx, row in enumerate(table, 1):
        cells = []
        for c_idx, key in enumerate(fieldnames, 1):
            value = csv_value(row.get(key, ""))
            ref = f"{column_name(c_idx)}{r_idx}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        xml_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )


def column_name(index: int) -> str:
    out = ""
    while index:
        index, remainder = divmod(index - 1, SPREADSHEET_COLUMN_BASE)
        out = chr(65 + remainder) + out
    return out
