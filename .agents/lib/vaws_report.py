"""Small file helpers for one-call business reports; no task or runtime state."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4


def report_directory(root: Path, skill: str, requested: Path | None = None) -> Path:
    if requested is not None:
        return requested.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return root / ".vaws-local" / "reports" / skill / f"{stamp}-{uuid4().hex[:8]}"


@contextmanager
def report_config(path: Path, *, root: Path, prefix: str):
    """Supply report format/version IDs internally; preserve business fields."""
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("business config must be a JSON object")
    config.setdefault("schema_version", 1)
    config.setdefault("run_id", f"{prefix}-{uuid4().hex[:12]}")
    directory = root / ".vaws-local" / "report-inputs"
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(suffix=".json", dir=directory)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False)
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)
