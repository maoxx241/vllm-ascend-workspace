#!/usr/bin/env python3
"""Asset writer for the v2 HTML report.

All detail data ships as ``assets/**/*.json.gz`` — compact JSON compressed
with stdlib ``gzip`` (deterministic: ``mtime=0``). ``assets/manifest.json``
indexes every asset so both the browser shell and tests can verify
completeness (every referenced file exists, every file is referenced).

Single-file mode base64-embeds the same gzipped payloads into the shell;
the browser inflates them with ``DecompressionStream('gzip')`` — no server
needed, at the cost of a ~1.37× base64 size penalty.
"""
from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
from typing import Any

from .payloads import MANIFEST_SCHEMA

#: Default single-file refusal threshold (estimated final HTML size).
DEFAULT_SINGLE_FILE_MAX_BYTES = 20 * 1024 * 1024


def dumps_compact(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def gzip_bytes(raw: bytes) -> bytes:
    # mtime=0 → deterministic bytes (stable diffs / hashes across runs).
    return gzip.compress(raw, compresslevel=6, mtime=0)


def serialize_asset(payload: Any) -> bytes:
    """Payload → gzipped bytes (the on-disk and embedded representation)."""
    return gzip_bytes(dumps_compact(payload))


def write_asset(report_dir: Path, rel_file: str, payload: Any) -> dict[str, Any]:
    """Write one ``.json.gz`` asset under ``report_dir`` and return its
    manifest entry."""
    data = serialize_asset(payload)
    path = report_dir / rel_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"file": rel_file, "bytes": len(data), "schema": payload.get("schema", "")}


def write_manifest(report_dir: Path, manifest: dict[str, Any]) -> Path:
    path = report_dir / "assets" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def build_manifest(
    *,
    title: str,
    renderer: str,
    generated_at: str,
    entries: dict[str, dict[str, Any]],
    single_file: bool,
) -> dict[str, Any]:
    """The asset index. ``entries`` maps a logical key (``l2/<class_id>``,
    ``l3/<class_id>/<layer_key>``, ``timeline/<rank_id>``, ``findings``) to
    ``{file, bytes, schema}``."""
    return {
        "schema": MANIFEST_SCHEMA,
        "html_renderer": renderer,
        "title": title,
        "generated_at": generated_at,
        "single_file": single_file,
        "assets": entries,
    }


def embed_assets(serialized: dict[str, bytes]) -> dict[str, str]:
    """``{asset_file_path: base64(gzipped_json)}`` for single-file mode.

    Keyed by the on-disk relative path (``assets/l2/<id>.json.gz`` …) so the
    browser resolves embedded payloads through the same ``entry.file`` lookup
    it uses for fetch mode.
    """
    return {key: base64.b64encode(data).decode("ascii") for key, data in serialized.items()}


def estimate_single_file_bytes(shell_overhead: int, serialized: dict[str, bytes]) -> int:
    """Estimated final HTML size: shell + base64 inflation (4/3) of assets."""
    return shell_overhead + sum((len(data) * 4 + 2) // 3 for data in serialized.values())


class SingleFileTooLargeError(ValueError):
    """Raised when single-file embedding would exceed the size threshold."""

    def __init__(self, estimated: int, threshold: int):
        self.estimated = estimated
        self.threshold = threshold
        super().__init__(
            f"single-file HTML 估算 {estimated / 1024 / 1024:.1f} MB，超过阈值 "
            f"{threshold / 1024 / 1024:.1f} MB；请改用默认的 assets 目录模式"
            f"（report.html + assets/），或调大 single_file_max_bytes。"
        )
